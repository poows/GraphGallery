import numpy as np
import tensorflow as tf
from tensorflow.keras import Model, Input
from tensorflow.keras.layers import Dropout
from tensorflow.keras.optimizers import Adam
from tensorflow.keras import regularizers
from tensorflow.keras.losses import SparseCategoricalCrossentropy

from graphgallery.nn.layers.tf_layers import MeanAggregator, GCNAggregator
from graphgallery.nn.models import SemiSupervisedModel
from graphgallery.sequence import SAGEMiniBatchSequence
from graphgallery.utils.decorators import EqualVarLength
from graphgallery import transforms as T


class GraphSAGE(SemiSupervisedModel):
    """
        Implementation of SAmple and aggreGatE Graph Convolutional Networks (GraphSAGE). 
        `Inductive Representation Learning on Large Graphs <https://arxiv.org/abs/1706.02216>`
        Tensorflow 1.x implementation: <https://github.com/williamleif/GraphSAGE>
        Pytorch implementation: <https://github.com/williamleif/graphsage-simple/>
    """

    def __init__(self, *graph, n_samples=(15, 5),
                 adj_transform="neighbor_sampler", attr_transform=None,
                 device='cpu:0', seed=None, name=None, **kwargs):
        """Create a SAmple and aggreGatE Graph Convolutional Networks (GraphSAGE) model.

        This can be instantiated in several ways:

            model = GraphSAGE(graph)
                with a `graphgallery.data.Graph` instance representing
                A sparse, attributed, labeled graph.

            model = GraphSAGE(adj_matrix, attr_matrix, labels)
                where `adj_matrix` is a 2D Scipy sparse matrix denoting the graph,
                 `attr_matrix` is a 2D Numpy array-like matrix denoting the node 
                 attributes, `labels` is a 1D Numpy array denoting the node labels.


        Parameters:
        ----------
        graph: An instance of `graphgallery.data.Graph` or a tuple (list) of inputs.
            A sparse, attributed, labeled graph.
        n_samples: List of positive integer. optional
            The number of sampled neighbors for each nodes in each layer. 
            (default :obj: `(15, 5)`, i.e., sample `15` first-order neighbors and 
            `5` sencond-order neighbors, and the radius for `GraphSAGE` is `2`)
        adj_transform: string, `transform`, or None. optional
            How to transform the adjacency matrix. See `graphgallery.transforms`
            (default: :obj:`'neighbor_sampler'`) 
        attr_transform: string, `transform`, or None. optional
            How to transform the node attribute matrix. See `graphgallery.transforms`
            (default :obj: `None`)
        device: string. optional 
            The device where the model is running on. You can specified `CPU` or `GPU` 
            for the model. (default: :str: `CPU:0`, i.e., running on the 0-th `CPU`)
        seed: interger scalar. optional 
            Used in combination with `tf.random.set_seed` & `np.random.seed` 
            & `random.seed` to create a reproducible sequence of tensors across 
            multiple calls. (default :obj: `None`, i.e., using random seed)
        name: string. optional
            Specified name for the model. (default: :str: `class.__name__`)
        kwargs: other customized keyword Parameters.

        """

        super().__init__(*graph, device=device, seed=seed, name=name, **kwargs)

        self.n_samples = n_samples
        self.adj_transform = T.get(adj_transform)
        self.attr_transform = T.get(attr_transform)
        self.process()

    def process_step(self):
        graph = self.graph
        # Dense matrix, shape [n_nodes, max_degree]
        adj_matrix = self.adj_transform(graph.adj_matrix)
        attr_matrix = self.attr_transform(graph.attr_matrix)

        # pad with a dummy zero vector
        attr_matrix = np.vstack(
            [attr_matrix, np.zeros(attr_matrix.shape[1], dtype=self.floatx)])

        self.feature_inputs, self.structure_inputs = T.astensors(
            attr_matrix, device=self.device), adj_matrix

    # use decorator to make sure all list arguments have the same length
    @EqualVarLength()
    def build(self, hiddens=[32], activations=['relu'], dropout=0.5,
              l2_norm=5e-4, lr=0.01, use_bias=True, output_normalize=False, aggrator='mean'):

        with tf.device(self.device):

            if aggrator == 'mean':
                Agg = MeanAggregator
            elif aggrator == 'gcn':
                Agg = GCNAggregator
            else:
                raise ValueError(
                    f"Invalid value of `aggrator`, allowed values (`'mean'`, `'gcn'`), but got `{aggrator}`.")

            x = Input(batch_shape=[None, self.graph.n_attrs],
                      dtype=self.floatx, name='attr_matrix')
            nodes = Input(batch_shape=[None], dtype=self.intx, name='nodes')
            neighbors = [Input(batch_shape=[None], dtype=self.intx, name=f'neighbors_{hop}')
                         for hop, n_sample in enumerate(self.n_samples)]

            aggrators = []
            for hidden, activation in zip(hiddens, activations):
                # you can use `GCNAggregator` instead
                aggrators.append(Agg(hidden, concat=True, activation=activation,
                                     use_bias=use_bias,
                                     kernel_regularizer=regularizers.l2(l2_norm)))

            aggrators.append(Agg(self.graph.n_classes, use_bias=use_bias))

            h = [tf.nn.embedding_lookup(x, node)
                 for node in [nodes, *neighbors]]
            for agg_i, aggrator in enumerate(aggrators):
                attribute_shape = h[0].shape[-1]
                for hop in range(len(self.n_samples) - agg_i):
                    neighbor_shape = [-1, self.n_samples[hop], attribute_shape]
                    h[hop] = aggrator(
                        [h[hop], tf.reshape(h[hop + 1], neighbor_shape)])
                    if hop != len(self.n_samples) - 1:
                        h[hop] = Dropout(rate=dropout)(h[hop])
                h.pop()

            h = h[0]
            if output_normalize:
                h = tf.nn.l2_normalize(h, axis=1)

            model = Model(inputs=[x, nodes, *neighbors], outputs=h)
            model.compile(loss=SparseCategoricalCrossentropy(from_logits=True),
                          optimizer=Adam(lr=lr), metrics=['accuracy'])

            self.model = model

    def train_sequence(self, index):
        index = T.asintarr(index)
        labels = self.graph.labels[index]
        sequence = SAGEMiniBatchSequence(
            [self.feature_inputs, self.structure_inputs, index], labels,
            n_samples=self.n_samples, device=self.device)
        return sequence

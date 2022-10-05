import h5py
import yaml
import numpy as np
import tensorflow as tf
from util import print_level
from keras.utils import Sequence
import matplotlib.pyplot as plt

class Data:
    def __init__(self, config_path):
        self.datasets = ['train', 'valid', 'test']
        self.config_path = config_path
        self.load_config()
        self.get_data_info()
        self.compute_param_norms()

        # If we have an upper limit on the number of boxes then this
        # sets our number of groups to use later (still use all the
        # groups to normalise the parameters)
        # n_groups = self.n_groups
        # groups = self.groups
        # self.n_groups = {ds: n_groups for ds in self.datasets}
        # self.groups = {ds: groups for ds in self.datasets}
        
        if self.max_n_box > 0:
            # Randomly shuffle all of the old boxes
            gidxs = np.random.choice(self.n_groups['train'],
                                     self.max_n_box,
                                     replace=False)
            assert(self.max_n_box == gidxs.shape[0]), 'More samples than allowed'
            self.n_groups['train'] = self.max_n_box
            self.n_groups['valid'] = self.max_n_box
            # Sample the new n_groups number of boxes from the shuffled data
            self.groups['train'] = self.groups['test'][gidxs]
            self.groups['valid'] = self.groups['test'][gidxs]


    def load_config(self):
        """Loads the YAML config file stored at config_path and stores
        in self.config
        
        """
        with open(self.config_path, 'r') as f:
            self.config = yaml.safe_load(f)
            self.batch_size = int(self.config['batch_size'])
            self.epochs = int(self.config['epochs'])
            self.verbose = bool(self.config['verbose'])
            self.out_dir = self.config['out_dir']
            self.param_keys = self.config['param_keys']
            self.max_n_slice = int(self.config['max_n_slice'])
            self.max_n_box = int(self.config['max_n_box'])
            self.generator = bool(self.config['generator'])

            assert ((self.max_n_slice < 0) or (self.max_n_box < 0)), 'Cannot use max_n_slice and max_n_box together'
            
            assert(self.config['mode'] in ['test', 'train', 'both'])

            # Set variables which dictate how the network runs
            self.train = True
            self.test = True
            if self.config['mode'] == 'train':
                self.test = False
            if self.config['mode'] == 'test':
                self.train = False
            
            print_level(f'read config file {self.config_path:s}',
                        1,
                        self.verbose)
            print_level(f'running in {self.config["mode"]} mode',
                        2,
                        self.verbose)
            print_level(f'using a batch size of {self.batch_size:d}',
                        2,
                        self.verbose)
            print_level(f'using {self.epochs:d} epochs',
                        2,
                        self.verbose)
            
            if self.max_n_box > 0:
                print_level(f'using a maximum of {self.max_n_box:d} boxes',
                            2,
                            self.verbose)
            elif self.max_n_slice > 0:
                print_level(f'using a maximum of {self.max_n_slice:d} boxes',
                            2,
                            self.verbose)
            else:
                print_level(f'using all available training data',
                            2,
                            self.verbose)

    def get_data_info(self):
        """Reads the properties of the HDF5 data file
        
        """
        self.n_params = len(self.param_keys)
        self.groups = {}
        self.n_groups = {}
        self.n_slices = {}
        self.dim = {}
        with h5py.File(self.config['data_path'], 'r') as hf:
            for ds in self.datasets:
                # For each dataset, we keep only the groups that
                # contain that dataset
                self.groups[ds] = np.array([group for group in hf.keys() if
                                            group+'/'+ds in hf])
                self.n_groups[ds] = len(self.groups[ds])
                g0 = self.groups[ds][0]
                self.n_slices[ds] = hf[g0+'/'+ds].shape[0]  # per group
                self.dim[ds] = (hf[g0+'/'+ds].shape[1],
                                hf[g0+'/'+ds].shape[2],
                                1)  # dimensions of a single input image

            # FIXME put a better assert here, also should probably
            # check other properties are consistent (though something
            # will have gone badly wrong in preprocessing if not)
            # assert(all([d == self.dim[self.datasets[0]] for d in self.dim]))
            self.dim = self.dim[self.datasets[0]]
            
        print_level(f'read HDF5 file {self.config["data_path"]:s}',
                    1,
                    self.verbose)
        print_level(f'using {self.n_params:d} params',
                    2,
                    self.verbose)
        for ds in self.datasets:
            print_level(f'{ds:s} has {self.n_groups[ds]:d} groups, where group each has {self.n_slices[ds]:d} {ds} slices',
                        3,
                        self.verbose)
        print_level(f'and each slice has dimension {self.dim}',
                    3,
                    self.verbose)
        
            
    def compute_param_norms(self):
        """Computes the mean and standard deviation of each of the
        parameters, for use in normalisation. This is done over /all/
        datasets.

        """
        print_level('computing parameter normalisations',
                    2,
                    self.verbose)

        n_groups_tot = sum(self.n_groups.values())
        params = np.zeros((n_groups_tot, self.n_params),
                          dtype=np.float32)

        with h5py.File(self.config['data_path'], 'r') as hf:
            # Just iterate over all the groups, we don't care which
            # dataset they are assigned to
            for i, group in enumerate(hf.keys()):
                for j, param_key in enumerate(self.param_keys):
                    params[i, j] = hf[group].attrs[param_key]

        mu = np.zeros((1, self.n_params))
        sig = np.zeros((1, self.n_params))

        for i in range(self.n_params):
            mu[0, i] = np.mean(params[:, i])
            sig[0, i] = np.std(params[:, i])
        
        self.norms = {'mu': mu,
                      'sig': sig}

        for i, param_key in enumerate(self.param_keys):
            print_level(f'{param_key}: mu = {self.norms["mu"][0, i]:.4f} sig = {self.norms["sig"][0, i]:.4f}',
                        3,
                        self.verbose)

            
    def get_shuffle_idxs(self, ds):
        """Generates a set of random indexes to shuffle the training
        and validation data, but only does this once. Also generates
        indices for shuffling the test data, but these won't be used.

        """
        shuffle_attr = 'shuffle_idxs'
        if hasattr(self, shuffle_attr):
            shuffle_idxs = getattr(self, shuffle_attr)
        else:
            shuffle_idxs = {}
            for _ds in self.datasets:
                n_choice = self.n_groups[_ds] * self.n_slices[_ds]
                shuffle_idxs[_ds] = np.random.choice(n_choice,
                                                     n_choice,
                                                     replace=False)
            setattr(self, shuffle_attr, shuffle_idxs)

        return shuffle_idxs[ds]


    def load_data(self, ds, shuffle=False):
        """Load all the data for a given dataset, returns input and
        output numpy arrays

        Parameters
        ----------
        ds : str
            Type of dataset to load up (e.g. 'train')

        shuffle : bool
            Whether to randomly shuffle the input and
            output data

        """

        print_level(f'loading {ds:s} data',
                    1,
                    self.verbose)
        
        ns = self.n_slices[ds]       # number of slices per group
        nt = ns * self.n_groups[ds]  # total number of slices used in this ds
        # print(nt, ns)
        x = np.zeros((nt, *self.dim), dtype=np.float32)      # input
        y = np.zeros((nt, self.n_params), dtype=np.float32)  # output

        with h5py.File(self.config['data_path'], 'r') as hf:
            # Iterate over all the groups being used for this ds
            for i, group in enumerate(self.groups[ds]):
                # Positions in the main output array
                i0 = i * ns
                i1 = (i + 1) * ns
                # print(i0, i1)
                
                # Temporaray work arrays
                _x = np.array(hf[group+'/'+ds],
                              dtype=np.float32).reshape((ns, *self.dim))
                _y = np.array([hf[group].attrs[param_key]
                               for param_key in self.param_keys],
                              dtype=np.float32).reshape((1, self.n_params))
                # These groups share output params
                _y = np.tile(_y, ns).reshape((ns, self.n_params))

                # Store
                x[i0:i1, :, :, :] = _x
                y[i0:i1, :] = _y

        # Normalise parameters
        print_level('normalising parameters',
                    2,
                    self.verbose)
        y = self.normalise_parameters(y)
        print_level(f'{self.param_keys[i]}: min = {y[:, i].min():.4f} max = {y[:, i].max():.4f} mean = {np.mean(y[:, i]):.4f}',
                    3,
                    self.verbose)

        if shuffle:
            shuffle_idxs = self.get_shuffle_idxs(ds)
            x = x[shuffle_idxs, :, :, :]
            y = y[shuffle_idxs, :]

        return x, y

    def normalise_parameters(self, y):
        """Normalise the output parameter array y, using the precomputed mean and standard deviation as y_norm = (y - mu) / sig

        Parameters
        ----------
        y : arr
            Output parameters

        Examples
        --------
        y = self.normalise_parameters(y)

        """
        for i in range(self.n_params):
            y[:, i] = (y[:, i] -
                       self.norms['mu'][0, i]) / self.norms['sig'][0, i]

        return y


class DataGenerator(Sequence):
    """Based on
    https://stanford.edu/~shervine/blog/keras-how-to-generate-data-on-the-fly"""

    def __init__(self, D, ds, shuffle=True):
        """A DataGenerator to provided batched data on-demand to a network

        Parameters
        ----------
        D : Data
            Instance of the Data class, which contains information
            about the data and network parameters
        ds : str
            Which dataset to load out of 'train', 'valid' and 'test'
        shuffle : bool
            Whether to shuffle the data between epochs, default is
            True

        Examples
        --------
        d = Data(config_path)
        x_train = DataGenerator(d, 'train')
        y_train = None
        x_valid = DataGenerator(d, 'valid')
        y_valid = None

        train_network(model, x_train=x_train, ...)

        """
        self.D = D
        self.ds = ds
        self.shuffle = shuffle
        self.gen_idxs()
        self.on_epoch_end()

    def __len__(self):
        """Returns the number of batches per epoch

        """
        return int(self.n_slices_tot // self.D.batch_size)

    def __getitem__(self, i):
        i0 = i * self.D.batch_size
        i1 = (i + 1) * self.D.batch_size
        idxs_batch = self.idxs[i0:i1, :]

        x, y = self.load_data(idxs_batch)

        return x, y

    def gen_idxs(self):
        """Generates a 2D array of indexes containing the group and
        position of every slice

        """
        self.n_slices_tot = self.D.n_slices[self.ds] * self.D.n_groups[self.ds]
        self.idxs = np.zeros(shape=(self.n_slices_tot, 2), dtype=int)  # int32 to save space?
        slice_idxs = np.arange(0, self.D.n_slices[self.ds], dtype=int)

        for i, g in enumerate(self.D.groups[self.ds]):
            i0 = i * self.D.n_slices[self.ds]  
            i1 = (i + 1) * self.D.n_slices[self.ds]

            self.idxs[i0:i1, 0] = int(g)      # group index
            self.idxs[i0:i1, 1] = slice_idxs  # slice index

    def load_data(self, idxs_batch):
        d = '{0:05d}/'
        x = np.zeros((self.D.batch_size, *self.D.dim),
                     dtype=np.float32)
        y = np.zeros((self.D.batch_size, self.D.n_params),
                     dtype=np.float32)

        with h5py.File(self.D.config['data_path'], 'r') as hf:
            # Iterate over the group and slice in the idxs for this batch
            for i, (g, s) in enumerate(zip(idxs_batch[:, 0],
                                           idxs_batch[:, 1])):
                x[i, :, :, 0] = np.array(hf[d.format(g)+self.ds])[s, :, :]
                y[i, :] = np.array([hf[d.format(g)].attrs[param_key]
                                    for param_key
                                    in self.D.param_keys],
                                   dtype=np.float32).reshape((1, self.D.n_params))

        # Normalise parameters
        y = self.D.normalise_parameters(y)

        return x, y

    def on_epoch_end(self):
        """Shuffle the slices at the end of each epoch

        """
        if self.shuffle:
            np.random.shuffle(self.idxs)

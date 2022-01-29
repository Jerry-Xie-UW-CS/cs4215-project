from torchvision import datasets, transforms
import random
import logging
from torch.utils.data import DistributedSampler, Dataset
from typing import Iterator
from collections import Counter
import numpy as np

class DistributedSamplerWrapper(DistributedSampler):
    indices = []
    epoch_size = 1.0
    def __init__(self, dataset: Dataset, num_replicas = None,
                 rank = None, seed = 0) -> None:
        super().__init__(dataset, num_replicas=num_replicas, rank=rank)

        self.client_id = rank - 1
        self.n_clients = num_replicas - 1
        self.n_labels = len(dataset.classes)
        self.seed = seed


    def order_by_label(self, dataset):
        # order the indices by label
        ordered_by_label = [[] for i in range(len(dataset.classes))]
        for index, target in enumerate(dataset.targets):
            ordered_by_label[target].append(index)

        return ordered_by_label

    def set_epoch_size(self, epoch_size: float) -> None:
        """ Sets the epoch size as relative to the local amount of data. 
        1.5 will result in the __iter__ function returning the available 
        indices with half appearing twice. 

        Args:
            epoch_size (float): relative size of epoch
        """
        self.epoch_size = epoch_size

    def __iter__(self) -> Iterator[int]:
        random.seed(self.rank+self.epoch)
        epochs_todo = self.epoch_size
        indices = []
        while(epochs_todo > 0.0):
            random.shuffle(self.indices)
            if epochs_todo >= 1.0:
                indices.extend(self.indices)
            else:
                end_index = int(round(len(self.indices)*epochs_todo))
                indices.extend(self.indices[:end_index])

            epochs_todo = epochs_todo - 1

        ratio = len(indices)/float(len(self.indices))
        np.testing.assert_almost_equal(ratio, self.epoch_size, decimal=2)

        return iter(indices)

    def __len__(self) -> int:
        return len(self.indices)

class N_Labels(DistributedSamplerWrapper):
    """
    A sampler that limits the number of labels per client
    The number of clients must <= than number of labels
    """

    def __init__(self, dataset, num_replicas, rank, args=(5, 42)):
        limit, seed = args
        super().__init__(dataset, num_replicas, rank, seed)

        num_copies = np.ceil((args[0] * self.n_clients) / self.n_labels)
        label_dict = {}
        for l in range(self.n_labels):
            label_dict[l] = num_copies

        def get_least_used_labels(l_dict: dict):
            label_list = [[k, v] for k, v in label_dict.items()]
            label_list[-1][1] = 0
            sorted_list = sorted(label_list, key=lambda x: x[1], reverse=True)
            # print('d')
            # label_list.sort(lambda x:x)

        def choice_n(l_dict: dict, n, seed_offset = 0):
            # get_least_used_labels(l_dict)
            labels = [k for k, v in label_dict.items() if v]
            # summed = sum([int(v) for k, v in label_dict.items() if v])
            # amounts = [float(v) / float(summed) for k, v in label_dict.items() if v]
            # # p = amounts / summed
            print(f'Available labels: {labels} choose {n}')
            # # np.random.seed(seed + seed_offset)
            # # @TODO: Error is in this section!
            # print(f'n={n}, labels={labels}, p={amounts}')
            # print(amounts)

            selected = np.random.choice(labels, n, replace=False)
            # print(selected)
            for k, v in l_dict.items():
                if k in selected:
                    # v -= 1
                    l_dict[k] -= 1
            return selected


        # print(f'N Clients={self.n_clients}')
        # print(f'Num_buckets={num_copies}')

        clients = list(range(self.n_clients))  # keeps track of which clients should still be given a label
        client_label_dict = {}
        ordered_list = list(range(self.n_labels)) * int(num_copies)

        # Old code
        # for idx, client_id in enumerate(clients):
        #     # client_label_dict[client_id] = []
        #     label_set = choice_n(label_dict, args[0], idx)
        #     client_label_dict[client_id] = label_set

        # Now code
        for idx, client_id in enumerate(clients):
            label_set = []
            for _ in range(args[0]):
                label_set.append(ordered_list.pop())
            client_label_dict[client_id] = label_set

        client_label_dict['rest'] = []
        # New code
        if len(ordered_list):
            client_label_dict['rest'] = ordered_list

        #     Old code
        # client_label_dict['rest'] = labels = [k for k, v in label_dict.items() if v]
        # for k, v in label_dict.items():
        #     for x in range(int(v)):
        #         client_label_dict['rest'].append(int(k))

        # Order data by label; split into N buckets and select indices based on the order found in the client-label-dict

        reverse_label_dict = {}
        for l in range(self.n_labels):
            reverse_label_dict[l] = []

        for k, v in client_label_dict.items():
            # print(f'client {k} has labels {v}')
            for l_c in v:
                reverse_label_dict[l_c].append(k)

        indices = []
        ordered_by_label = self.order_by_label(dataset)
        indices_per_client = {}
        for c in clients:
            indices_per_client[c] = []

        rest_indices = []
        for group, label_list in enumerate(ordered_by_label):
            splitted = np.array_split(label_list, num_copies)
            client_id_to_distribute = reverse_label_dict[group]
            for split_part in splitted:
                client_key = client_id_to_distribute.pop()
                if client_key == 'rest':
                    rest_indices.append(split_part)
                else:
                    indices_per_client[client_key].append(split_part)
            # for split_part in splitted:
        # @TODO: Fix this part in terms of code cleanness. Could be written more cleanly
        if len(rest_indices):
            rest_indices = np.concatenate(rest_indices)
            rest_splitted = np.array_split(rest_indices, len(indices_per_client))

            for k, v in indices_per_client.items():
                v.append(rest_splitted.pop())
                indices_per_client[k] = np.concatenate(v)
        else:
            rest_indices = np.ndarray([])
            for k, v in indices_per_client.items():
                indices_per_client[k] = np.concatenate(v)

        indices = indices_per_client[self.client_id]
        random.seed(seed + self.client_id)  # give each client a unique shuffle
        random.shuffle(indices)  # shuffle indices to spread the labels

        self.indices = indices

        # labels_per_client = int(np.floor(self.n_labels / self.n_clients))
        # remaining_labels = self.n_labels - labels_per_client
        # labels = list(range(self.n_labels))  # list of labels to distribute
        # clients = list(range(self.n_clients))  # keeps track of which clients should still be given a label
        # client_labels = [set() for n in range(self.n_clients)]  # set of labels given to each client
        # random.seed(seed)  # seed, such that the same result can be obtained multiple times
        # print(client_labels)
        #
        # label_order = random.sample(labels, len(labels))
        # client_label_dict = {}
        # for client_id in clients:
        #     client_label_dict[client_id] = []
        #     for _ in range(labels_per_client):
        #         chosen_label = label_order.pop()
        #         client_label_dict[client_id].append(chosen_label)
        #         client_labels[client_id].add(chosen_label)
        # client_label_dict['rest'] = label_order
        #
        #
        #
        # indices = []
        # ordered_by_label = self.order_by_label(dataset)
        # labels = client_label_dict[self.client_id]
        # for label in labels:
        #     n_samples = int(len(ordered_by_label[label]))
        #     clients = [c for c, s in enumerate(client_labels) if label in s]  # find out which clients have this label
        #     index = clients.index(self.client_id)  # find the position of this client
        #     start_index = index * n_samples  # inclusive
        #     if rank == self.n_clients:
        #         end_index = len(ordered_by_label[label])  # exclusive
        #     else:
        #         end_index = start_index + n_samples  # exclusive
        #
        #     indices += ordered_by_label[label][start_index:end_index]
        #
        # # Last part is uniform sampler
        # rest_indices = []
        # for l in client_label_dict['rest']:
        #     rest_indices += ordered_by_label[l]
        # filtered_rest_indices = rest_indices[self.rank:self.total_size:self.num_replicas]
        # indices += filtered_rest_indices
        # random.seed(seed + self.client_id)  # give each client a unique shuffle
        # random.shuffle(indices)  # shuffle indices to spread the labels
        #
        # self.indices = indices


class LimitLabelsSamplerFlex(DistributedSamplerWrapper):
    """
    A sampler that limits the number of labels per client
    The number of clients must <= than number of labels
    """

    def __init__(self, dataset, num_replicas, rank, args=(5, 42)):
        limit, seed = args
        super().__init__(dataset, num_replicas, rank, seed)

        labels_per_client = int(np.floor(self.n_labels / self.n_clients))
        remaining_labels = self.n_labels - labels_per_client
        labels = list(range(self.n_labels))  # list of labels to distribute
        clients = list(range(self.n_clients))  # keeps track of which clients should still be given a label
        client_labels = [set() for n in range(self.n_clients)]  # set of labels given to each client
        random.seed(seed)  # seed, such that the same result can be obtained multiple times
        print(client_labels)

        label_order = random.sample(labels, len(labels))
        client_label_dict = {}
        for client_id in clients:
            client_label_dict[client_id] = []
            for _ in range(labels_per_client):
                chosen_label = label_order.pop()
                client_label_dict[client_id].append(chosen_label)
                client_labels[client_id].add(chosen_label)
        client_label_dict['rest'] = label_order

        indices = []
        ordered_by_label = self.order_by_label(dataset)
        labels = client_label_dict[self.client_id]
        for label in labels:
            n_samples = int(len(ordered_by_label[label]))
            clients = [c for c, s in enumerate(client_labels) if label in s]  # find out which clients have this label
            index = clients.index(self.client_id)  # find the position of this client
            start_index = index * n_samples  # inclusive
            if rank == self.n_clients:
                end_index = len(ordered_by_label[label])  # exclusive
            else:
                end_index = start_index + n_samples  # exclusive

            indices += ordered_by_label[label][start_index:end_index]

        # Last part is uniform sampler
        rest_indices = []
        for l in client_label_dict['rest']:
            rest_indices += ordered_by_label[l]
        filtered_rest_indices = rest_indices[self.rank:self.total_size:self.num_replicas]
        indices += filtered_rest_indices
        random.seed(seed + self.client_id)  # give each client a unique shuffle
        random.shuffle(indices)  # shuffle indices to spread the labels

        self.indices = indices

class LimitLabelsSampler(DistributedSamplerWrapper):
    """
    A sampler that limits the number of labels per client
    """
    def __init__(self, dataset, num_replicas, rank, args=(5, 42)):
        limit, seed = args
        super().__init__(dataset, num_replicas, rank, seed)
        
        if self.n_clients % self.n_labels != 0:
            logging.error(
                "multiples of {} clients are needed for the 'limiting-labels' data distribution method, {} does not work".format(
                    self.n_labels, self.n_clients))
            return

        n_occurrences = limit * int(self.n_clients / self.n_labels)  # number of occurrences of each label
        counters = [n_occurrences] * self.n_clients  # keeps track of which labels still can be given out
        labels = list(range(self.n_labels))  # list of labels to distribute
        clients = list(range(self.n_clients))  # keeps track of which clients should still be given a label
        client_labels = [set() for n in range(self.n_clients)]  # set of labels given to each client
        random.seed(seed)  # seed, such that the same result can be obtained multiple times

        while labels:
            # pick a random label
            label = random.choice(labels)
            counters[label] -= 1  # decrement counter of this label
            if counters[label] == 0:  # if needed, remove label
                labels.remove(label)

            # check which clients the label can be given to
            selectable = [i for i in clients if not label in client_labels[i]]
            client = None

            if not selectable:
                # poor choice, let's fix this -> swap two labels
                # conditions for swapping:
                #   sets of labels A, B, with B incomplete, remaining label l that is not possible to give to B, s.t.:
                #       (1) l not in A
                #       (2) exists label l' in A but not in B
                #   l, l' can be swapped

                client = random.choice(clients)  # label can not be given to this client
                for c, s in enumerate(client_labels):
                    if len(s) == limit:  # this a completed set
                        if label not in s:  # label can be given to this client (1)
                            subset = s.difference(client_labels[client])  # remove labels client already has (2...)
                            if subset:  # subset is not empty (2 continued):
                                l = min(subset)  # get a swappable label (in a deterministic way), and swap labels
                                client_labels[c].remove(l)
                                client_labels[c].add(label)
                                client_labels[client].add(l)
                                break
            else:  # normal operation, pick a rondom selectable client
                client = random.choice(selectable)
                client_labels[client].add(label)

            # check if this client has been given the maximum number of labels
            if len(client_labels[client]) == limit:
                clients.remove(client)

        # now we have a set of labels for each client
        # client with rank=rank now needs to be given data
        # all clients get the same amount of data, the first portion is given to client with rank 1, the second to rank 2, etc

        labels = client_labels[self.client_id]
        logging.info("Client {} gets labels {}".format(self.rank, client_labels[self.client_id]))
        indices = []
        ordered_by_label = self.order_by_label(dataset)
        for label in labels:
            n_samples = int(len(ordered_by_label[label]) / n_occurrences)
            clients = [c for c, s in enumerate(client_labels) if label in s]  # find out which clients have this label
            index = clients.index(self.client_id)  # find the position of this client
            start_index = index * n_samples  # inclusive
            if rank == self.n_clients:
                end_index = len(ordered_by_label[label])  # exclusive
            else:
                end_index = start_index + n_samples  # exclusive

            indices += ordered_by_label[label][start_index:end_index]

        random.seed(seed + self.client_id)  # give each client a unique shuffle
        random.shuffle(indices)  # shuffle indices to spread the labels

        self.indices = indices

class Probability_q_Sampler(DistributedSamplerWrapper):
    """
    Clients are divided among M groups, with M being the number of labels.
    A sample with label m is than given to a member of group m with probability q,
    and to any other group with probability (1-q)/(m-1)

    side effect of this method is that the reported loss on the test dataset becomes somewhat meaningless...logging.info("distribution in client with rank {}: {}".format(rank, Counter(labels)))
    """

    def __init__(self, dataset, num_replicas, rank, args=(0.5, 42)):
        q, seed = args
        super().__init__(dataset, num_replicas, rank, seed)
        
        if self.n_clients % self.n_labels != 0:
            logging.error(
                "multiples of {} clients are needed for the 'probability-q-sampler' data distribution method, {} does not work".format(
                    self.n_labels,self.n_clients))
            return

        # divide data among groups
        counter = 0  # for dividing data within a group
        group_id = self.client_id % self.n_labels
        group_clients = [client for client in range(self.n_clients) if client % self.n_labels == group_id]
        indices = []
        random.seed(seed)
        ordered_by_label = self.order_by_label(dataset)
        for group, label_list in enumerate(ordered_by_label):
            for sample_idx in label_list:
                rnd_val = random.random()
                if rnd_val < q:
                    if group == group_id:
                        if group_clients[counter] == self.client_id:
                            indices.append(sample_idx)
                        counter = (counter + 1) % len(group_clients)
                else:
                    others = [grp for grp in range(self.n_labels) if grp != group]
                    if random.choice(others) == group_id:
                        if group_clients[counter] == self.client_id:
                            indices.append(sample_idx)
                        counter = (counter + 1) % len(group_clients)

        labels = [dataset.targets[i] for i in indices]
        logging.info("nr of samplers in client with rank {}: {}".format(rank, len(indices)))
        logging.info("distribution in client with rank {}: {}".format(rank, Counter(labels)))

        random.seed(seed + self.client_id)  # give each client a unique shuffle
        random.shuffle(indices)  # shuffle indices to spread the labels

        self.indices = indices

class DirichletSampler(DistributedSamplerWrapper):
    """ Generates a (non-iid) data distribution by sampling the dirichlet distribution. Dirichlet constructs a
    vector of length num_clients, that sums to one. Decreasing alpha results in a more non-iid data set. 
    This distribution method results in both label and quantity skew. 
    """
    def __init__(self, dataset: Dataset, num_replicas = None,
                 rank = None, args = (0.5, 42)) -> None:
        alpha, seed = args
        super().__init__(dataset, num_replicas=num_replicas, rank=rank, seed=seed)

        np.random.seed(seed)
        indices = []
        ordered_by_label = self.order_by_label(dataset)
        for labels in ordered_by_label:
            n_samples = len(labels)
            # generate an allocation by sampling dirichlet, which results in how many samples each client gets
            allocation = np.random.dirichlet([alpha] * self.n_clients) * n_samples 
            allocation = allocation.astype(int)
            start_index = allocation[0:self.client_id].sum()
            end_index = 0
            if self.client_id + 1 == self.n_clients:  # last client
                end_index = n_samples
            else:
                end_index = start_index + allocation[self.client_id]

            selection = labels[start_index:end_index]
            indices.extend(selection)

        labels = [dataset.targets[i] for i in indices]
        logging.info("nr of samplers in client with rank {}: {}".format(rank, len(indices)))
        logging.info("distribution in client with rank {}: {}".format(rank, Counter(labels)))

        random.seed(seed + self.client_id)  # give each client a unique shuffle
        random.shuffle(indices)  # shuffle indices to spread the labels

        self.indices = indices

class UniformSampler(DistributedSamplerWrapper):
    def __init__(self, dataset, num_replicas=None, rank=None, seed=0):
        super().__init__(dataset, num_replicas=num_replicas, rank=rank, seed=seed)
        indices = list(range(len(self.dataset)))
        self.indices = indices[self.rank:self.total_size:self.num_replicas]

def get_sampler(dataset, args):
    sampler = None
    if args.get_distributed():
        method = args.get_sampler()
        args.get_logger().info(
            "Using {} sampler method, with args: {}".format(method, args.get_sampler_args()))
        
        if method == "uniform":
            sampler = UniformSampler(dataset, num_replicas=args.get_world_size(), rank=args.get_rank())
        elif method == "q sampler":
            sampler = Probability_q_Sampler(dataset, num_replicas=args.get_world_size(), rank=args.get_rank(),
                                            args=args.get_sampler_args())
        elif method == "limit labels":
            sampler = LimitLabelsSampler(dataset, num_replicas=args.get_world_size(), rank=args.get_rank(),
                                         args=args.get_sampler_args())
        elif method == "limit labels flex":
            sampler = LimitLabelsSamplerFlex(dataset, num_replicas=args.get_world_size(), rank=args.get_rank(),
                                         args=args.get_sampler_args())
        elif method == "n labels":
            sampler = N_Labels(dataset, num_replicas=args.get_world_size(), rank=args.get_rank(),
                                         args=args.get_sampler_args())
        elif method == "dirichlet":
            sampler = DirichletSampler(dataset, num_replicas=args.get_world_size(), rank=args.get_rank(),
                                       args=args.get_sampler_args())
        else:  # default
            args().get_logger().warning("Unknown sampler " + method + ", using uniform instead")
            sampler = UniformSampler(dataset, num_replicas=args.get_world_size(), rank=args.get_rank())

    return sampler

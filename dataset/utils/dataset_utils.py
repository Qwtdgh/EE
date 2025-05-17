# PFLlib: Personalized Federated Learning Algorithm Library
# Copyright (C) 2021  Jianqing Zhang

# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 2 of the License, or
# (at your option) any later version.

# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.

# You should have received a copy of the GNU General Public License along
# with this program; if not, write to the Free Software Foundation, Inc.,
# 51 Franklin Street, Fifth Floor, Boston, MA 02110-1301 USA.

import os
import torch.utils
import torch.utils.data
import ujson
import numpy as np
import gc
import torch
from sklearn.model_selection import train_test_split
import pickle
import scipy.io as sio

batch_size = 32
train_ratio = 0.8 # merge original training set and valid set, then split it manually. 
alpha = 1000 # for Dirichlet distribution

def check(config_path, train_path, valid_path, num_clients, niid=False, 
        balance=True, partition=None):
    # check existing dataset
    if os.path.exists(config_path):
        with open(config_path, 'r') as f:
            config = ujson.load(f)
        if config['num_clients'] == num_clients and \
            config['non_iid'] == niid and \
            config['balance'] == balance and \
            config['partition'] == partition and \
            config['alpha'] == alpha and \
            config['batch_size'] == batch_size:
            print("\nDataset already generated.\n")
            return True

    dir_path = os.path.dirname(train_path)
    if not os.path.exists(dir_path):
        os.makedirs(dir_path)
    dir_path = os.path.dirname(valid_path)
    if not os.path.exists(dir_path):
        os.makedirs(dir_path)

    return False

def separate_data(data, num_clients, num_classes, niid=False, balance=False, partition=None, class_per_client=None, attention_mask=None):
    X = [[] for _ in range(num_clients)]
    y = [[] for _ in range(num_clients)]
    m = [[] for _ in range(num_clients)]
    statistic = [[] for _ in range(num_clients)]

    dataset_content, dataset_label = data
    # guarantee that each client must have at least one batch of data for validing. 
    least_samples = int(min(batch_size / (1-train_ratio), len(dataset_label) / num_clients / 2))

    dataidx_map = {}

    if not niid:
        partition = 'pat'
        class_per_client = num_classes

    if partition == 'pat':
        idxs = np.array(range(len(dataset_label)))
        idx_for_each_class = []
        for i in range(num_classes):
            idx_for_each_class.append(idxs[dataset_label == i])

        class_num_per_client = [class_per_client for _ in range(num_clients)]
        for i in range(num_classes):
            selected_clients = []
            for client in range(num_clients):
                if class_num_per_client[client] > 0:
                    selected_clients.append(client)
            selected_clients = selected_clients[:int(np.ceil((num_clients/num_classes)*class_per_client))]

            num_all_samples = len(idx_for_each_class[i])
            num_selected_clients = len(selected_clients)
            num_per = num_all_samples / num_selected_clients
            if balance:
                num_samples = [int(num_per) for _ in range(num_selected_clients-1)]
            else:
                num_samples = np.random.randint(max(num_per/10, least_samples/num_classes), num_per, num_selected_clients-1).tolist()
            num_samples.append(num_all_samples-sum(num_samples))

            idx = 0
            for client, num_sample in zip(selected_clients, num_samples):
                if client not in dataidx_map.keys():
                    dataidx_map[client] = idx_for_each_class[i][idx:idx+num_sample]
                else:
                    dataidx_map[client] = np.append(dataidx_map[client], idx_for_each_class[i][idx:idx+num_sample], axis=0)
                idx += num_sample
                class_num_per_client[client] -= 1

    elif partition == "dir":
        # https://github.com/IBM/probabilistic-federated-neural-matching/blob/master/experiment.py
        min_size = 0
        K = num_classes
        N = len(dataset_label)

        try_cnt = 1
        while min_size < least_samples:
            if try_cnt > 1:
                print(f'Client data size does not meet the minimum requirement {least_samples}. Try allocating again for the {try_cnt}-th time.')
            if try_cnt == 5: break
            idx_batch = [[] for _ in range(num_clients)]
            for k in range(K):
                idx_k = np.where(dataset_label == k)[0]
                np.random.shuffle(idx_k)
                proportions = np.random.dirichlet(np.repeat(alpha, num_clients))
                proportions = np.array([p*(len(idx_j)<N/num_clients) for p,idx_j in zip(proportions,idx_batch)])
                proportions = proportions/proportions.sum()
                proportions = (np.cumsum(proportions)*len(idx_k)).astype(int)[:-1]
                idx_batch = [idx_j + idx.tolist() for idx_j,idx in zip(idx_batch,np.split(idx_k,proportions))]
                min_size = min([len(idx_j) for idx_j in idx_batch])
            try_cnt += 1
            print(try_cnt)
            
        for j in range(num_clients):
            dataidx_map[j] = idx_batch[j]
    else:
        raise NotImplementedError

    # assign data
    for client in range(num_clients):
        idxs = dataidx_map[client]
        X[client] = dataset_content[idxs]
        y[client] = dataset_label[idxs]
        if attention_mask is not None:
            m[client] = attention_mask[idxs]

        for i in np.unique(y[client]):
            statistic[client].append((int(i), int(sum(y[client]==i))))
            

    del data
    # gc.collect()

    for client in range(num_clients):
        print(f"Client {client}\t Size of data: {len(X[client])}\t Labels: ", np.unique(y[client]))
        print(f"\t\t Samples of labels: ", [i for i in statistic[client]])
        print("-" * 50)

    if attention_mask is not None:
        del attention_mask
        return X, y, m, statistic
    else: return X, y, statistic 

def split_data(X, y):
    # Split dataset
    train_data, valid_data = [], []
    num_samples = {'train':[], 'valid':[]}

    for i in range(len(y)):
        X_train, X_valid, y_train, y_valid = train_test_split(
            X[i], y[i], train_size=train_ratio, shuffle=True)

        train_data.append({'x': X_train, 'y': y_train})
        num_samples['train'].append(len(y_train))
        valid_data.append({'x': X_valid, 'y': y_valid})
        num_samples['valid'].append(len(y_valid))

    print("Total number of samples:", sum(num_samples['train'] + num_samples['valid']))
    print("The number of train samples:", num_samples['train'])
    print("The number of valid samples:", num_samples['valid'])
    print()
    del X, y
    # gc.collect()

    return train_data, valid_data

def save_file(config_path, train_path, valid_path, train_data, valid_data, num_clients, 
                num_classes, statistic, niid=False, balance=True, partition=None):
    config = {
        'num_clients': num_clients, 
        'num_classes': num_classes, 
        'non_iid': niid, 
        'balance': balance, 
        'partition': partition, 
        'Size of samples for labels in clients': statistic, 
        'alpha': alpha, 
        'batch_size': batch_size, 
    }

    # gc.collect()
    print("Saving to disk.\n")

    for idx, train_dict in enumerate(train_data):
        with open(train_path + str(idx) + '.pkl', 'wb') as f:
            pickle.dump(train_dict, f)
    for idx, valid_dict in enumerate(valid_data):
        with open(valid_path + str(idx) + '.pkl', 'wb') as f:
            pickle.dump(valid_dict, f)
    with open(config_path, 'w') as f:
        ujson.dump(config, f)

    print("Finish generating dataset.\n")


def save_origin_file(path, set, set_len):

    loader = torch.utils.data.DataLoader(
        set, batch_size=set_len, shuffle=False)

    for idx, data in enumerate(loader, 0):
        set.data, set.targets = data
    
    dict = {'x': set.data.cpu().detach().numpy(), 'y': set.targets.cpu().detach().numpy()}
    
    with open(path + '.pkl', 'wb') as f:
        np.savez_compressed(f, data=dict)
        


def load_tsv(data_path):
    list_data_dict = []
    with open(data_path, 'r') as f:
        try:
            for line_id, line in enumerate(f):
                if line_id == 0:
                    continue
                else:
                    line = line.strip().split('\t')
                    if len(line) == 2:
                        new_item = dict(
                            label=int(line[0]),
                            input_id=line[1]
                        )
                    else:
                        new_item = dict(
                            label=int(line[0]),
                            input_id=(line[1], line[2])
                        )
                    list_data_dict.append(new_item)
        except Exception as e:
            print(line)
        f.close()
    return list_data_dict


def load_np(file):
    with open(file, 'rb') as f:
        dict = np.load(f, allow_pickle=True)['data'].tolist()
    return dict

def load_pkl(file):
    with open(file, 'rb') as f:
        dict = pickle.load(f, encoding='bytes')
    return dict

def load_mat(file):
    return sio.loadmat(file)
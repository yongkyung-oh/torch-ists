#-*- coding:utf-8 -*-

import os
import sys
import copy
import math
import time
import datetime
import json
import pickle
import random

import urllib.request
import zipfile

import numpy as np
import pandas as pd
import matplotlib
import matplotlib.pyplot as plt
import sklearn.model_selection
from sklearn.metrics import *

from tqdm import tqdm

import torch
import torch.nn.functional as F
import torch.optim as optim
from torch import nn, Tensor
from torch.utils.data import Dataset, DataLoader

# setup seed
def seed_everything(seed):
    os.environ['PYTHONHASHSEED'] = str(seed)
    np.random.seed(seed)
    random.seed(seed)
    torch.manual_seed(seed)
    torch.random.manual_seed(seed)
    torch.cuda.manual_seed(seed)
    torch.cuda.manual_seed_all(seed)
#     torch.backends.cudnn.deterministic = True
#     torch.backends.cudnn.benchmark = True
    
SEED = 0
seed_everything(SEED)

# CUDA for PyTorch
# os.environ["CUDA_DEVICE_ORDER"] = "PCI_BUS_ID"
# os.environ["CUDA_VISIBLE_DEVICES"] = "0"

use_cuda = torch.cuda.is_available()
device = torch.device("cuda" if use_cuda else "cpu")

import torchcde
import torchsde

from torch_ists import get_data, preprocess
from torch_ists import ists_dataset, ists_classifier, train, evaluate 

import ray
from ray import tune
from ray.tune.schedulers import ASHAScheduler

## list seq base for ists
model_name_list = [
    'cnn', 'cnn-3', 'cnn-5', 'cnn-7', 
    'rnn', 'lstm', 'gru', 'gru-simple', 'grud',
    'bilstm', 'tlstm', 'plstm', 'tglstm',
    'transformer', 'sand', 'mtan', 'miam',
    'gru-dt', 'gru-d', 'gru-ode', 'ode-rnn', 'ode-lstm',
    'neuralcde', 'neuralcde-l', 'neuralcde-r', 'neuralcde-c', 'neuralcde-h', 
    'neuralrde-1', 'neuralrde-2', 'neuralrde-3', 
    'ancde', 'exit', 'leap',
    'latentsde', 'latentsde-kl', 'neuralsde-x', 'neuralsde-y', 'neuralsde-z', 
]

## list of flow models
flow_models = [
    [x for y in [['neuralflow_{}_{}'.format(i,j) for i in ['x', 'y', 'z']] for j in ['n', 'r', 'g', 'c']] for x in y],
    [x for y in [['neuralflowcde_{}_{}'.format(i,j) for i in ['x', 'y', 'z']] for j in ['n', 'r', 'g', 'c']] for x in y],
    [x for y in [['neuralmixture_{}_{}'.format(i,j) for i in ['x', 'y', 'z']] for j in ['n', 'r', 'g', 'c']] for x in y],
    [x for y in [['neuralcontrolledflow_{}_{}'.format(i,j) for i in ['x', 'y', 'z']] for j in ['n', 'r', 'g', 'c']] for x in y],
]
# flow_models = [x for y in flow_models for x in y]
model_name_list = model_name_list + [x for y in flow_models for x in y]

## list of sde models
sde_models = [['neuralsde_{:1d}_{:02d}'.format(i,j) for i in range(7)] for j in range(20)]
sde_models = [x for y in sde_models for x in y]
model_name_list = model_name_list + sde_models


if not os.path.exists('params'):
    os.mkdir('params')

# set model
def tune_model(data_name, missing_rate, model_name, model_config, EPOCHS=100, SEED=SEED):
    print(data_name)
    
    # load data
    X, Y = get_data(data_name)
    num_data = X.shape[0]
    num_dim = X.shape[1]
    seq_len = X.shape[2]
    num_class = len(np.unique(Y))
    
    # set batch_size by the number of data
    batch_size = 2**4
    for i in range(4,8):
        if 2**i > num_data/2:
            break
        batch_size = 2**i

    # set learning params
    if model_config['lr'] is None:
        lr = 1e-3 * (batch_size / 2**4)
    else:
        lr = model_config['lr']
    
    # check model_name and settings
    if model_name in ['gru-dt', 'gru-d', 'gru-ode', 'ode-rnn', 'ncde', 'ancde', 'exit']:
        interpolate = 'natural'
    else:
        interpolate = 'hermite'

    if model_name in ['gru-dt', 'gru-d', 'ode-rnn']:
        use_intensity = True
    else:
        use_intensity = False

    ## data split    
    seed_everything(SEED)
        
    # 0.7/0.15/0.15 train/val/test split
    train_idx, test_idx = sklearn.model_selection.train_test_split(range(len(Y)), train_size=0.7, shuffle=True, stratify=Y, random_state=SEED)
    valid_idx, test_idx = sklearn.model_selection.train_test_split(test_idx, train_size=0.5, shuffle=True, stratify=Y[test_idx], random_state=SEED)

    # load dataset
    X_missing, X_mask, X_delta, coeffs = preprocess(X, missing_rate=missing_rate, interpolate=interpolate, use_intensity=use_intensity)
    X_train = X_missing[train_idx]

    out = []
    for Xi, train_Xi in zip(X_missing.unbind(dim=-1), X_train.unbind(dim=-1)):
        train_Xi_nonan = train_Xi.masked_select(~torch.isnan(train_Xi))
        mean = train_Xi_nonan.mean()  # compute statistics using only training data.
        std = train_Xi_nonan.std()
        out.append((Xi - mean) / (std + 1e-5))
    X_missing_norm = torch.stack(out, dim=-1)

    train_dataset = ists_dataset(Y, X_missing_norm, X_mask, X_delta, coeffs, train_idx)
    valid_dataset = ists_dataset(Y, X_missing_norm, X_mask, X_delta, coeffs, valid_idx)
    test_dataset = ists_dataset(Y, X_missing_norm, X_mask, X_delta, coeffs, test_idx)

    train_batch = DataLoader(train_dataset, batch_size=batch_size, shuffle=True, drop_last=False)
    valid_batch = DataLoader(valid_dataset, batch_size=batch_size, shuffle=True, drop_last=False)
    test_batch = DataLoader(test_dataset, batch_size=batch_size, shuffle=False, drop_last=False)
    
    # set params
    if model_name in ['lstm-mean', 'gru-mean', 'gru-simple', 'grud', 'tlstm', 'plstm', 'tglstm']:
        num_layers = model_config['num_layers']
        num_hidden_layers = None
    elif model_name in ['sand', 'mtan', 'miam']:
        num_layers = 1 
        num_hidden_layers = None
    else:
        num_layers = 1
        num_hidden_layers = model_config['num_layers']
    
    # get model_kwargs
    model_kwargs = {
        'hidden_dim': model_config['hidden_dim'], 
        'hidden_hidden_dim': model_config['hidden_dim'], 
        'num_layers': num_layers, 
        'num_hidden_layers': num_hidden_layers,
    }
    
    # set ancde
    if not os.path.exists(os.path.join(os.path.join(os.getcwd(),'ancde'))):
        os.mkdir(os.path.join(os.path.join(os.getcwd(),'ancde')))
    ancde_path = os.path.join(os.getcwd(), 'ancde/{}_{}.npy'.format(data_name, str(SEED))) # for ancde model

    # set model
    model = ists_classifier(model_name=model_name, input_dim=num_dim, seq_len=seq_len, num_class=num_class, dropout=0.1, use_intensity=use_intensity, 
                            method='euler', file=ancde_path, device='cuda', **model_kwargs)
    model = model.to(device)

    # set loss & optimizer
    criterion = nn.CrossEntropyLoss() 
    optimizer = optim.Adam(model.parameters(), lr=lr, weight_decay=lr*0.01)
    scheduler = optim.lr_scheduler.StepLR(optimizer, step_size=5, gamma=0.5)

    best_loss = np.infty
    best_model_wts = copy.deepcopy(model.state_dict())
    patient = 0

    for e in tqdm(range(EPOCHS)):
        train_loss = train(model, optimizer, criterion, train_batch, interpolate, use_intensity, device)
        valid_loss = evaluate(model, criterion, valid_batch, interpolate, use_intensity, device)
        test_loss = evaluate(model, criterion, test_batch, interpolate, use_intensity, device)

        if e % 10 == 0:
            print(e, train_loss, valid_loss, test_loss)

        if valid_loss < best_loss:
            best_loss = valid_loss
            best_model_wts = copy.deepcopy(model.state_dict())
            patient = 0
        else:
            patient += 1

        if (e > 10) & (patient > 5):
            break

        scheduler.step()

#         with tune.checkpoint_dir(e) as checkpoint_dir:
#             out_name = '_'.join([data_name, str(missing_rate), model_name, str(SEED)])
#             path = os.path.join(checkpoint_dir, out_name)
#             torch.save((model.state_dict(), optimizer.state_dict()), path)

        valid_loss = np.nan_to_num(valid_loss,np.infty) # ignore
        tune.report(loss=valid_loss)
    print("Finished Training")


def optimize(data_name, missing_rate, model_name, num_samples=20, max_num_epochs=10):
    model_config = {
        "data_name": data_name, 
        "missing_rate": missing_rate, 
        "model_name": model_name, 
        # "batch_size": None, # determine by the number of the data
        "lr": tune.loguniform(1e-4, 1e-2),
        "hidden_dim": tune.choice([16, 32, 64, 128]),
        "num_layers": tune.choice([1, 2, 3, 4]),
    }
    
    scheduler = ASHAScheduler(
        max_t=max_num_epochs,
        grace_period=1,
        reduction_factor=2)
    
    def optimize_model(model_config):
        tune_model(data_name=data_name, missing_rate=missing_rate, model_name=model_name, 
                   model_config=model_config, EPOCHS=max_num_epochs, SEED=SEED)
    
    result = tune.run(
        tune.with_parameters(optimize_model),
        resources_per_trial={"cpu": 16, "gpu": 0.3},
        config=model_config,
        metric="loss",
        mode="min",
        num_samples=num_samples,
        scheduler=scheduler,
        stop={"training_iteration": 10}
    )
    
    best_trial = result.get_best_trial("loss", "min", "last")
    print("Best trial config: {}".format(best_trial.config))
    print("Best trial final validation loss: {}".format(best_trial.last_result["loss"]))
    
    # save params
    if not os.path.exists(os.path.join('params', data_name)):
        os.mkdir(os.path.join('params', data_name))
    
    out_name = '_'.join([data_name, model_name])
    with open(os.path.join('params', data_name, out_name), 'wb') as f:
        pickle.dump(best_trial.config, f)
    
    return best_trial


##### run all
data_info = pd.read_csv('dataset_summary_multivariate.csv', index_col=0)
data_info['totalsize'] = data_info['trainsize'] + data_info['testsize']
data_info = data_info.loc[(data_info['totalsize'] < 10000) & (data_info['num_dim'] < 100) & (data_info['max_len'] < 5000)]
data_info = data_info.sort_values('totalsize')
data_name_multivariate = data_info['problem'].tolist()

data_info = pd.read_csv('dataset_summary_univariate.csv', index_col=0)
data_info['totalsize'] = data_info['trainsize'] + data_info['testsize']
data_info = data_info.loc[(data_info['totalsize'] < 10000) & (data_info['num_dim'] < 100) & (data_info['max_len'] < 5000)]
data_info = data_info.sort_values('totalsize')
data_name_univariate = data_info['problem'].tolist() 
    
data_name_list = data_name_multivariate + data_name_univariate


# optimize parameters
for data_name in data_name_list:
    for model_name in model_name_list:
        
        out_name = '_'.join([data_name, model_name])
        if os.path.exists(os.path.join('params', data_name, out_name)):
            continue
            
        try:
            best_trial = optimize(data_name=data_name, missing_rate=0, model_name=model_name, num_samples=20, max_num_epochs=10)
        except:
            continue



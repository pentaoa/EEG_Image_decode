import os

import torch
import torch.optim as optim
from torch.nn import CrossEntropyLoss
from torch.nn import functional as F
from torch.optim import Adam
from torch.utils.data import DataLoader

os.environ["CUDA_VISIBLE_DEVICES"] = "0"

from itertools import combinations
import clip
import matplotlib.pyplot as plt
import numpy as np
import torch.nn as nn
import torchvision.transforms as transforms
import tqdm
from cls_eegdatasets import EEGDataset
from eegencoder import eeg_encoder
from einops.layers.torch import Rearrange, Reduce
from sklearn.metrics import confusion_matrix
from torch.utils.data import DataLoader, Dataset
import random
from utils import wandb_logger
import torch.nn.functional as F
from einops.layers.torch import Rearrange
from lavis.models.clip_models.loss import ClipLoss
from braindecode.models import EEGNetv4



device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')


class eegEncoder(nn.Module):
    def __init__(self):
        super().__init__()
        self.device = device
        self.shape = (17, 80)
        self.eegnet = EEGNetv4(
            in_chans=self.shape[0],
            n_classes=512,   #输出embedding维度 512x512 
            input_window_samples=self.shape[1],
            final_conv_length='auto',
            pool_mode='mean',
            F1=8,
            D=20,
            F2=160,
            kernel_length=4,
            third_kernel_size=(4, 2),
            drop_prob=0.25
        )
    def forward(self, data):
        data = data.unsqueeze(0)
        data = data.reshape(data.shape[1], data.shape[2], data.shape[3], data.shape[0])
        print(data.shape)
        prediction = self.eegnet(data)
        return prediction

def train_model(model, dataloader, optimizer, device, text_features_all, img_features_all):
    model.train()
    text_features_all = text_features_all.to(device).float() # (n_cls, d)
    img_features_all = img_features_all.to(device).float()
    total_loss = 0
    correct = 0
    total = 0
    for batch_idx, (eeg_data, labels, text, text_features, img, img_features) in enumerate(dataloader):
        eeg_data = eeg_data.to(device)
        text_features = text_features.to(device).float()
        img_features = img_features.to(device).float()
        labels = labels.to(device)
        # print("labels", labels.shape)        
        # print("eeg_data", eeg_data.shape)        
        optimizer.zero_grad()
        
        eeg_features = model.forward(eeg_data)  #eeg_data torch.Size([512, 17, 51])
        eeg_features.to(device).float()
        # print("eeg_features", eeg_features.shape)    
        logit_scale = nn.Parameter(torch.ones([]) * np.log(1 / 0.07))
        loss_func = ClipLoss()    
        loss = loss_func(eeg_features, img_features, logit_scale)
        loss.backward()

        optimizer.step()
        total_loss += loss.item()    
        # print("text_features_all", text_features_all.shape)   #text_features_all torch.Size([1654, 512])
        logits = logit_scale * eeg_features @ img_features.T # (n_batch, n_cls) #estimate torch.Size([512, 1654])
        predicted = torch.argmax(logits, dim=1) # (n_batch, ) \in {0, 1, ..., n_cls-1}

        batch_size = predicted.shape[0]
        total += batch_size
        correct += (predicted == labels).sum().item()

    average_loss = total_loss / (batch_idx+1)
    accuracy = correct / total
    return average_loss, accuracy

def evaluate_model(model, dataloader, device, text_features_all, img_features_all, k):
    model.eval()
    text_features_all = text_features_all.to(device).float()
    img_features_all = img_features_all.to(device).float()
    total_loss = 0
    correct = 0
    total = 0

    # 获取所有独特的类别
    all_labels = set(range(text_features_all.size(0)))

    with torch.no_grad():
        for batch_idx, (eeg_data, labels, text, text_features, img, img_features) in enumerate(dataloader):
            eeg_data = eeg_data.to(device)
            text_features = text_features.to(device).float()
            labels = labels.to(device)
            
            eeg_features = model.forward(eeg_data)            
            logit_scale = nn.Parameter(torch.ones([]) * np.log(1 / 0.07))
            loss_func = ClipLoss() 
            loss = loss_func(eeg_features, text_features, logit_scale)#estimate(512, 1654)
            total_loss += loss.item()


            for idx, label in enumerate(labels):
                # 先从除了正确类别之外的类别中选择 k-1 个
                possible_classes = list(all_labels - {label.item()})
                selected_classes = random.sample(possible_classes, k-1) + [label.item()]
                selected_text_features = text_features_all[selected_classes]
                
                # 计算对应的 logits
                logits_single = logit_scale * eeg_features[idx] @ selected_text_features.T

                # 获取预测的类别
                predicted_label = selected_classes[torch.argmax(logits_single).item()]

                if predicted_label == label.item():
                    correct += 1

                total += 1

    average_loss = total_loss / (batch_idx+1)
    accuracy = correct / total
    return average_loss, accuracy

def main_train_loop(model, train_dataloader, test_dataloader, optimizer, device, 
                    text_features_train_all, text_features_test_all, img_features_train_all, img_features_test_all, config, logger=None):
    logger = wandb_logger(config) if logger else None
    logger.watch(model,logger) 
    
    train_losses, train_accuracies = [], []
    test_losses, test_accuracies = [], []
    v2_accs = []
    v4_accs = []
    v10_accs = []

    best_accuracy = 0.0
    best_model_weights = None
    best_epoch_info = {}

    for epoch in range(config['epochs']):
        # 训练模型
        train_loss, train_accuracy = train_model(model, train_dataloader, optimizer, device, text_features_train_all, img_features_train_all)
        train_losses.append(train_loss)
        train_accuracies.append(train_accuracy)

        # 评估模型
        test_loss, test_accuracy = evaluate_model(model, test_dataloader, device, text_features_test_all, img_features_test_all,k=200)
        _, v2_acc = evaluate_model(model, test_dataloader, device, text_features_test_all, img_features_test_all, k = 2)
        _, v4_acc = evaluate_model(model, test_dataloader, device, text_features_test_all, img_features_test_all, k = 4)
        _, v10_acc = evaluate_model(model, test_dataloader, device, text_features_test_all, img_features_test_all, k = 10)
        test_losses.append(test_loss)
        test_accuracies.append(test_accuracy)
        v2_accs.append(v2_acc)
        v4_accs.append(v4_acc)
        v10_accs.append(v10_acc)

        logger.log({
            "Train Loss": train_loss,
            "Train Accuracy": train_accuracy,
            "Test Loss": test_loss,
            "Test Accuracy": test_accuracy,
            "v2 Accuracy": v2_acc,
            "v4 Accuracy": v4_acc,
            "v10 Accuracy": v10_acc,
            "Epoch": epoch
        })

        print(f"Epoch {epoch + 1}/{config['epochs']} - Train Loss: {train_loss:.4f}, Train Accuracy: {train_accuracy:.4f}, Test Loss: {test_loss:.4f}, Test Accuracy: {test_accuracy:.4f}")
        print(f"Epoch {epoch + 1}/{config['epochs']} - v2 Accuracy:{v2_acc} - v4 Accuracy:{v4_acc} - v10 Accuracy:{v10_acc}")
    
# 创建5个子图
    fig, axs = plt.subplots(3, 2, figsize=(10, 15))

    # 损失图
    axs[0, 0].plot(train_losses, label='Train Loss')
    axs[0, 0].plot(test_losses, label='Test Loss')
    axs[0, 0].legend()
    axs[0, 0].set_title("Loss Curve")

    # 总体正确率图
    axs[0, 1].plot(train_accuracies, label='Train Accuracy')
    axs[0, 1].plot(test_accuracies, label='Test Accuracy')
    axs[0, 1].legend()
    axs[0, 1].set_title("Accuracy Curve")

    # 2分类正确率图
    axs[1, 0].plot(v2_accs, label='2-class Accuracy')
    axs[1, 0].legend()
    axs[1, 0].set_title("2-Class Accuracy Curve")

    # 4分类正确率图
    axs[1, 1].plot(v4_accs, label='4-class Accuracy')
    axs[1, 1].legend()
    axs[1, 1].set_title("4-Class Accuracy Curve")

    # 10分类正确率图
    axs[2, 0].plot(v10_accs, label='10-class Accuracy')
    axs[2, 0].legend()
    axs[2, 0].set_title("10-Class Accuracy Curve")

    # 构造你要注释的字符串信息
    info_text = (f"Best Model Info (from Epoch {best_epoch_info['epoch']}):\n"
                f"Train Loss: {best_epoch_info['train_loss']:.4f}\n"
                f"Train Accuracy: {best_epoch_info['train_accuracy']:.4f}\n"
                f"Test Loss: {best_epoch_info['test_loss']:.4f}\n"
                f"Test Accuracy: {best_epoch_info['test_accuracy']:.4f}\n"
                f"v2_acc:{best_epoch_info['v2_acc']:.4f}\n"
                f"v4_acc:{best_epoch_info['v4_acc']:.4f}\n"
                f"v10_acc:{best_epoch_info['v10_acc']:.4f}")

    axs[2, 1].axis('off')  
    axs[2, 1].text(0.5, 0.5, info_text, fontsize=10, ha='center', va='center', transform=axs[2, 1].transAxes)


    # 添加大标题
    plt.suptitle('best_model_lr=3e-4_img_eegnet', fontsize=16, y=1.05)
    plt.savefig('best_model_lr=3e-4_img_eegnet.png')
    logger.log({"Plots": logger.Image(plt)})
    logger.finish()

def main():
    config = {
        # 'project': 'eeg_clf',
        # 'entity': 'vis-obj',
        # 'name': 'lr=3e-4_img_eegnet', 
        'project': 'BCI',
        'entity': 'sustech_rethinkingbci',
        'name': '', 
        'lr': 3e-4, 
        'epochs': 50, 
        'batch_size': 512, 
        'logger': True, 
    }

   # Instantiate the dataset and dataloader
    data_path = '/home/geek/Workspace/BCI/Data/THINGS/EEG/osfstorage-archive'  # Replace with the path to your data
    train_dataset = EEGDataset(data_path, train=True)
    test_dataset = EEGDataset(data_path, train=False)
    train_loader = DataLoader(train_dataset, batch_size=512, shuffle=True, num_workers=0, drop_last=True)
    test_loader = DataLoader(test_dataset, batch_size=512, shuffle=True, num_workers=0, drop_last=True)
    
    text_features_train_all = train_dataset.text_features
    text_features_test_all = test_dataset.text_features
    img_features_train_all = train_dataset.img_features
    img_features_test_all = test_dataset.img_features
    
    
    # 模型和优化器定义
    model = eegEncoder()

    optimizer = torch.optim.AdamW(model.parameters(), lr=3e-4)
    
    print('number of parameters:', sum([p.numel() for p in model.parameters()]))

    # 确定使用的设备
    device = torch.device("cuda:0" if torch.cuda.is_available() else "cpu")
    model.to(device)
    main_train_loop(model, train_loader, test_loader, optimizer, device, 
                    text_features_train_all, text_features_test_all, img_features_train_all, img_features_test_all, config, logger=config['logger'])
    
if __name__ == '__main__':
    main()
import pickle as pickle
import os
from turtle import forward
import pandas as pd
import torch
import sklearn
import numpy as np
from sklearn.metrics import accuracy_score
from transformers import AutoTokenizer, AutoConfig, Trainer, TrainingArguments
from transformers import AutoModel
from load_data import *
# import wandb
import torch.nn as nn
import random
from sadice import SelfAdjDiceLoss

def seed_everything(seed):
    torch.manual_seed(seed)
    torch.cuda.manual_seed(seed)
    torch.cuda.manual_seed_all(seed)  # if use multi-GPU
    torch.backends.cudnn.deterministic = True
    torch.backends.cudnn.benchmark = False
    np.random.seed(seed)
    random.seed(seed)


# BiLSTM
class Model_BiLSTM(nn.Module):

  def __init__(self, MODEL_NAME):
    super().__init__()
    self.model_config =  AutoConfig.from_pretrained(MODEL_NAME)
    self.model_config.num_labels = 30
    self.model = AutoModel.from_pretrained(MODEL_NAME, config = self.model_config)
    self.hidden_dim = self.model_config.hidden_size
    self.lstm= nn.LSTM(input_size= self.hidden_dim, hidden_size= self.hidden_dim, num_layers= 1, batch_first= True, bidirectional= True)

    self.fc = nn.Linear(self.hidden_dim * 2, self.model_config.num_labels)
  
  def forward(self, input_ids, attention_mask):
    output = self.model(input_ids=input_ids, attention_mask=attention_mask).last_hidden_state
    # (batch, max_len, hidden_dim)

    hidden, (last_hidden, last_cell) = self.lstm(output)
    output = torch.cat((last_hidden[0], last_hidden[1]), dim=1)
    # hidden : (batch, max_len, hidden_dim * 2)
    # last_hidden : (2, batch, hidden_dim)
    # output : (batch, hidden_dim * 2)

    logits = self.fc(output)
    # logits : (batch, num_labels)

    return {'logits' : logits}

class CustomTrainer(Trainer):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
    
    def compute_loss(self, model, inputs, return_outputs= False):
        device= torch.device('cuda:0' if torch.cuda.is_available else 'cpu:0')
        labels= inputs.pop('labels')
        # print(labels)
        # forward pass
        outputs= model(**inputs)
        
        # 인덱스에 맞춰서 과거 ouput을 다 저장
        if self.args.past_index >=0:
            self._past= outputs[self.args.past_index]
            
        # compute custom loss (suppose one has 3 labels with different weights)

        # 1) CE Loss
        custom_loss= torch.nn.CrossEntropyLoss().to(device)
        loss= custom_loss(outputs['logits'], labels)    
        return (loss, outputs) if return_outputs else loss
        
        # 2) Dice Loss
        # criterion = SelfAdjDiceLoss()
        # dice_loss = criterion(outputs['logits'], labels).to(device)
        # return (dice_loss, outputs) if return_outputs else dice_loss


       



def seed_everything(seed):
    torch.manual_seed(seed)
    torch.cuda.manual_seed(seed)
    torch.cuda.manual_seed_all(seed)  # if use multi-GPU
    torch.backends.cudnn.deterministic = True
    torch.backends.cudnn.benchmark = False
    np.random.seed(seed)
    random.seed(seed)

def klue_re_micro_f1(preds, labels):
    """KLUE-RE micro f1 (except no_relation)"""
    label_list = ['no_relation', 'org:top_members/employees', 'org:members',
       'org:product', 'per:title', 'org:alternate_names',
       'per:employee_of', 'org:place_of_headquarters', 'per:product',
       'org:number_of_employees/members', 'per:children',
       'per:place_of_residence', 'per:alternate_names',
       'per:other_family', 'per:colleagues', 'per:origin', 'per:siblings',
       'per:spouse', 'org:founded', 'org:political/religious_affiliation',
       'org:member_of', 'per:parents', 'org:dissolved',
       'per:schools_attended', 'per:date_of_death', 'per:date_of_birth',
       'per:place_of_birth', 'per:place_of_death', 'org:founded_by',
       'per:religion']
    no_relation_label_idx = label_list.index("no_relation")
    label_indices = list(range(len(label_list)))
    label_indices.remove(no_relation_label_idx)
    return sklearn.metrics.f1_score(labels, preds, average="micro", labels=label_indices) * 100.0


def klue_re_auprc(probs, labels):
    """KLUE-RE AUPRC (with no_relation)"""
    labels = np.eye(30)[labels]
    score = np.zeros((30,))
    for c in range(30):
        targets_c = labels.take([c], axis=1).ravel()
        preds_c = probs.take([c], axis=1).ravel()
        precision, recall, _ = sklearn.metrics.precision_recall_curve(targets_c, preds_c)
        score[c] = sklearn.metrics.auc(recall, precision)
    return np.average(score) * 100.0


def compute_metrics(pred):
  """ validation을 위한 metrics function """
  labels = pred.label_ids
  preds = pred.predictions.argmax(-1)
  probs = pred.predictions

  # calculate accuracy using sklearn's function
  f1 = klue_re_micro_f1(preds, labels)
  auprc = klue_re_auprc(probs, labels)
  acc = accuracy_score(labels, preds) # 리더보드 평가에는 포함되지 않습니다.
  return {
      'micro f1 score': f1,
      'auprc' : auprc,
      'accuracy': acc,
  }


def label_to_num(label):
  """ 주어진 pickle 파일로 부터 label->num list를 불러와 train_dataset(DataFrame)의 label column의 값에 대응하는 숫자를 list에 담아 전달합니다.

  Args:
      label (DataFrame.values): train_dataset(DataFrame)의 label column의 값

  Returns:
      list: 대응하는 숫자
  """  
  num_label = []
  with open('dict_label_to_num.pkl', 'rb') as f:
    dict_label_to_num = pickle.load(f)
  for v in label:
    num_label.append(dict_label_to_num[v])
  
  return num_label

def train():

  # load model and tokenizer
  MODEL_NAME = "klue/roberta-large"
  tokenizer = AutoTokenizer.from_pretrained(MODEL_NAME)
  added_token_num = tokenizer.add_special_tokens({"additional_special_tokens":["[LOC]", "[DAT]", "[NOH]", "[PER]", "[ORG]", "[POH]"]})
  
  # load dataset
  train_dataset = load_data("/opt/ml/dataset/train/train.csv")

  train_label = label_to_num(train_dataset['label'].values)

  # tokenizing dataset
  tokenized_train = tokenized_dataset(train_dataset, tokenizer)

  # make dataset for pytorch.
  RE_train_dataset = RE_Dataset(tokenized_train, train_label)


  device = torch.device('cuda:0' if torch.cuda.is_available() else 'cpu')
  
  model =  Model_BiLSTM(MODEL_NAME)
  model.model.resize_token_embeddings(tokenizer.vocab_size + added_token_num)
  

  model.to(device)
 
  # 사용한 option 외에도 다양한 option들이 있습니다.
  # https://huggingface.co/transformers/main_classes/trainer.html#trainingarguments 참고해주세요.
  training_args = TrainingArguments(
    output_dir='./results',          # output directory
    save_strategy='epoch',
    save_total_limit=1,              # number of total save model.
    num_train_epochs=5,              # total number of training epochs
    learning_rate=6e-5,               # learning_rate
    per_device_train_batch_size=32,  # batch size per device during training
    gradient_accumulation_steps=2,   # gradient accumulation factor
    per_device_eval_batch_size=64,   # batch size for evaluation
    fp16=True,
    warmup_ratio = 0.1,

    weight_decay=0.01,               # strength of weight decay
    label_smoothing_factor=0.1,
    lr_scheduler_type = 'cosine',
    logging_dir='./logs',            # directory for storing logs
    logging_steps=100,              # log saving step.
    evaluation_strategy='epoch', # evaluation strategy to adopt during training
                                # `no`: No evaluation during training.
                                # `steps`: Evaluate every `eval_steps`.
                                # `epoch`: Evaluate every end of epoch.
    load_best_model_at_end = True,
    report_to = 'wandb',
    # run name은 실험자명과 주요 변경사항을 기입합니다. 
    run_name = 'kiwon-len=256/Acm=2/label_sm=0.1/lr=6e-5/sch=cos/loss=nll/seed=14'

  )

  trainer = CustomTrainer(
    model=model,                         # the instantiated 🤗 Transformers model to be trained
    args=training_args,                  # training arguments, defined above
    train_dataset=RE_train_dataset,         # training dataset
    eval_dataset=RE_dev_dataset,             # evaluation dataset
    compute_metrics=compute_metrics,         # define metrics function
    callbacks = [EarlyStoppingCallback(early_stopping_patience=2)]
  )


  trainer.train()
  torch.save(model.state_dict(), os.path.join('./best_model', 'pytorch_model.bin'))


def main():
  train()

if __name__ == '__main__':
  wandb.init(project="KLUE")
  # run name은 실험자명과 주요 변경사항을 기입합니다. 
  wandb.run.name = 'kiwon-len=256/Acm=2/label_sm=0.1/lr=6e-5/sch=cos/loss=nll/seed=14'
  seed_everything(14) 

  main()


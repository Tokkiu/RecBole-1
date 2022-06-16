# -*- coding: utf-8 -*-
# @Time    : 2022/02/24 10:43
# @Author  : Jingqi Gao
# @Email   : mrgao.ary@gmail.com
"""
FISSA: Fusing Item Similarity Models with Self-Attention Networks for Sequential Recommendation
################################################

Reference:
    Jing Lin, Weike Pan, and Zhong Ming. 2020. FISSA: Fusing Item Similarity
    Models with Self-Attention Networks for Sequential Recommendation. In
    Fourteenth ACM Conference on Recommender Systems (RecSys ’20), September
    22–26, 2020, Virtual Event, Brazil. ACM, New York, NY, USA, 10 pages.
    https://doi.org/10.1145/3383313.3412247

"""

import numpy as np
import torch
import torch.nn as nn
import torch.nn.functional as F
from torch.nn.init import xavier_normal_

from recbole.model.abstract_recommender import SequentialRecommender
from recbole.model.loss import BPRLoss
from recbole.utils import InputType
from recbole.model.layers import TransformerEncoder


class FISSA(SequentialRecommender):

    def __init__(self, config, dataset):
        super(FISSA, self).__init__(config, dataset)

        # load dataset info
        self.n_users = dataset.user_num
        self.n_items = dataset.item_num

        # load parameters info
        self.device = config["device"]
        self.loss_type = config['loss_type']
        self.n_layers = config['n_layers']
        self.n_heads = config['n_heads']
        self.hidden_size = config['hidden_size']  # same as embedding_size
        self.inner_size = config['inner_size']  # the dimensionality in feed-forward layer
        self.hidden_dropout_prob = config['hidden_dropout_prob']
        self.attn_dropout_prob = config['attn_dropout_prob']
        self.hidden_act = config['hidden_act']
        self.layer_norm_eps = config['layer_norm_eps']

        if self.loss_type == 'BPR':
            self.loss_fct = BPRLoss()
        elif self.loss_type == 'CE':
            self.loss_fct = nn.CrossEntropyLoss()
        else:
            raise NotImplementedError("Make sure 'loss_type' in ['BPR', 'CE']!")

        self.D = config['hidden_size']
        self.initializer_range = config['initializer_range']

        self.w1 = self._init_weight((self.D, self.D))
        self.w2 = self._init_weight((self.D, self.D))
        self.q_s = self._init_weight(self.D)
        self.gating_lr = torch.nn.Linear(self.D * 3, 1)
        self.gating_sig = torch.nn.Sigmoid()

        self.item_embedding = nn.Embedding(self.n_items, self.D, padding_idx=0)
        self.position_embedding = nn.Embedding(self.max_seq_length + 1, self.D)  # add mask_token at the last

        self.LayerNorm = nn.LayerNorm(self.hidden_size, eps=self.layer_norm_eps)
        self.dropout = nn.Dropout(self.hidden_dropout_prob)
        self.d1 = nn.Dropout(self.hidden_dropout_prob)
        self.trm_encoder = TransformerEncoder(
            n_layers=self.n_layers,
            n_heads=self.n_heads,
            hidden_size=self.hidden_size,
            inner_size=self.inner_size,
            hidden_dropout_prob=self.hidden_dropout_prob,
            attn_dropout_prob=self.attn_dropout_prob,
            hidden_act=self.hidden_act,
            layer_norm_eps=self.layer_norm_eps
        )
        # parameters initialization
        self.apply(self._init_weights)

    def _init_weight(self, shape):
        mat = np.random.normal(0, self.initializer_range, shape)
        return torch.tensor(mat, dtype=torch.float32, requires_grad=True).to(self.device)

    def _init_weights(self, module):
        if isinstance(module, nn.Embedding):
            xavier_normal_(module.weight)
        elif isinstance(module, nn.LayerNorm):
            module.bias.data.zero_()
            module.weight.data.fill_(1.0)

    def calculate_loss(self, interaction):
        item_seq = interaction[self.ITEM_SEQ]
        item_seq_len = interaction[self.ITEM_SEQ_LEN]
        pos_items = interaction[self.POS_ITEM_ID]

        seq_x, seq_y = self.forward(item_seq, item_seq_len)
        last_item_emb = self.gather_indexes(self.item_embedding(item_seq), item_seq_len-1)
        if self.loss_type == 'BPR':
            neg_items = interaction[self.NEG_ITEM_ID]
            pos_items_emb = self.item_embedding(pos_items).unsqueeze(1)
            neg_items_emb = self.item_embedding(neg_items).unsqueeze(1)
            seq_output_pos = self.cal_final(seq_x, seq_y, last_item_emb, pos_items_emb)
            seq_output_neg = self.cal_final(seq_x, seq_y, last_item_emb, neg_items_emb)

            pos_score = torch.sum(seq_output_pos * pos_items_emb, dim=-1)  # [B]
            neg_score = torch.sum(seq_output_neg * neg_items_emb, dim=-1)  # [B]
            loss = self.loss_fct(pos_score, neg_score)
            return loss
        else:  # self.loss_type = 'CE'
            test_item_emb = self.item_embedding.weight
            test_item_emb_can = test_item_emb.unsqueeze(0).repeat(seq_x.size(0), 1, 1)
            seq_output = self.cal_final(seq_x, seq_y, last_item_emb, test_item_emb_can)
            logits = torch.sum(seq_output * test_item_emb_can, dim=-1)  # [B]
            loss = self.loss_fct(logits, pos_items)
            return loss

    def cal_final(self, seq_x, seq_y, last_embedding, candidates_embedding):
        isg_input = torch.cat((last_embedding, seq_y), 1)
        isg_input = isg_input.unsqueeze(1).repeat((1, candidates_embedding.size(1), 1))
        isg_input = torch.cat((isg_input, candidates_embedding), 2)
        g = self.gating_sig(self.gating_lr(isg_input))
        seq_x_can = seq_x.unsqueeze(1).repeat(1, candidates_embedding.size(1), 1)
        seq_y_can = seq_y.unsqueeze(1).repeat(1, candidates_embedding.size(1), 1)
        seq = seq_x_can * g + seq_y_can * (1 - g)
        return seq

    def predict(self, interaction):
        item_seq = interaction[self.ITEM_SEQ]
        item_seq_len = interaction[self.ITEM_SEQ_LEN]
        last_item_emb = self.gather_indexes(self.item_embedding(item_seq), item_seq_len-1)

        test_item = interaction[self.ITEM_ID]
        seq_x, seq_y = self.forward(item_seq, item_seq_len)
        test_item_emb = self.item_embedding(test_item).unsqueeze(1)
        seq_output = self.cal_final(seq_x, seq_y, last_item_emb, test_item_emb)
        scores = torch.sum(seq_output * test_item_emb, dim=-1)
        return scores

    def get_attention_mask(self, item_seq):
        """Generate bidirectional attention mask for multi-head attention."""
        attention_mask = (item_seq > 0).long()
        extended_attention_mask = attention_mask.unsqueeze(1).unsqueeze(2)  # torch.int64
        # bidirectional mask
        extended_attention_mask = extended_attention_mask.to(dtype=next(self.parameters()).dtype)  # fp16 compatibility
        extended_attention_mask = (1.0 - extended_attention_mask) * -10000.0
        return extended_attention_mask

    def forward(self, item_seq, item_seq_len):
        item_embedding = self.item_embedding(item_seq).to(self.device)  # [B, N, D]

        position_ids = torch.arange(item_seq.size(1), dtype=torch.long, device=item_seq.device)
        position_ids = position_ids.unsqueeze(0).expand_as(item_seq)
        position_embedding = self.position_embedding(position_ids)

        input_emb = item_embedding + position_embedding
        input_emb = self.LayerNorm(input_emb)
        input_emb = self.dropout(input_emb)
        extended_attention_mask = self.get_attention_mask(item_seq)
        trm_output = self.trm_encoder(input_emb, extended_attention_mask, output_all_encoded_layers=True)
        x_l = trm_output[-1]
        x_l = self.gather_indexes(x_l, item_seq_len - 1)

        x = torch.matmul(item_embedding, self.w1)
        x = torch.matmul(x, self.q_s)
        a = F.softmax(x, dim=1)
        x = torch.matmul(item_embedding, self.w2)
        y = a.unsqueeze(2).repeat(1, 1, self.D) * x
        y = self.d1(y.sum(dim=1))

        return x_l, y

    def full_sort_predict(self, interaction):
        item_seq = interaction[self.ITEM_SEQ]
        item_seq_len = interaction[self.ITEM_SEQ_LEN]
        last_item_emb = self.gather_indexes(self.item_embedding(item_seq), item_seq_len-1)
        seq_x, seq_y = self.forward(item_seq, item_seq_len)
        test_items_emb = self.item_embedding.weight
        test_items_emb_can = test_items_emb.unsqueeze(0).repeat(seq_x.size(0), 1, 1)
        seq_output = self.cal_final(seq_x, seq_y, last_item_emb, test_items_emb_can)
        scores = torch.sum(seq_output * test_items_emb_can, dim=-1)
        return scores
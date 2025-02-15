import torch
import torch.nn as nn
from datasets.data_utils import collate_fn, get_pixel_features
from models.resnet import ResNetBackbone
from models.corner_models import HeatCorner
from models.edge_models import HeatEdge
from models.corner_to_edge import get_infer_edge_pairs
from infer import corner_nms, postprocess_preds
import numpy as np


class HEAT(nn.Module):
    def __init__(self, input_dim=128, hidden_dim=256, num_feature_levels=4):
        super().__init__()

        self.backbone = ResNetBackbone()
        strides = self.backbone.strides
        num_channels = self.backbone.num_channels

        self.corner_model = HeatCorner(input_dim, hidden_dim, num_feature_levels, backbone_strides=strides,
                                       backbone_num_channels=num_channels)
        self.edge_model = HeatEdge(input_dim, hidden_dim, num_feature_levels, backbone_strides=strides,
                                   backbone_num_channels=num_channels)

    def forward(self, image):
        image_feats, feat_mask, all_image_feats = self.backbone(image)

        # get the positional encodings for all pixels
        pixels, pixel_features = get_pixel_features(image_size=512)

        pixel_features = pixel_features.unsqueeze(0).repeat(image.shape[0], 1, 1, 1).to(image.device)
        preds_s1 = self.corner_model(image_feats, feat_mask, pixel_features, pixels, all_image_feats)

        c_outputs = preds_s1
    	# get predicted corners
        c_outputs_np = c_outputs[0].detach().cpu().numpy()
        pos_indices = np.where(c_outputs_np >= 0.01)
        pred_corners = pixels[pos_indices]
        pred_confs = c_outputs_np[pos_indices]
        pred_corners, pred_confs = corner_nms(pred_corners, pred_confs, image_size=c_outputs.shape[1])

        pred_corners, pred_confs, edge_coords, edge_mask, edge_ids = get_infer_edge_pairs(pred_corners, pred_confs)
        edge_coords, edge_mask, edge_ids = edge_coords.to(image.device), edge_mask.to(image.device), edge_ids.to(image.device)
        # edge_coords = edge_coords.to(image.device)

        corner_nums = torch.tensor([len(pred_corners)]).to(image.device)
        max_candidates = torch.stack([corner_nums.max() * 3] * len(corner_nums), dim=0)

        all_pos_ids = set()
        all_edge_confs = dict()

        for tt in range(3):
    	    if tt == 0:
    	        gt_values = torch.zeros_like(edge_mask).long()
    	        gt_values[:, :] = 2

    	    # run the edge model
    	    s1_logits, s2_logits_hb, s2_logits_rel, selected_ids, s2_mask, s2_gt_values = self.edge_model(image_feats, feat_mask,
    	                                                                                             	  pixel_features,
    	                                                                                             	  edge_coords, edge_mask,
    	                                                                                             	  gt_values, corner_nums,
    	                                                                                             	  max_candidates,
    	                                                                                             	  True)

    	    num_total = s1_logits.shape[2]
    	    num_selected = selected_ids.shape[1]
    	    num_filtered = num_total - num_selected

    	    s1_preds = s1_logits.squeeze().softmax(0)
    	    s2_preds_rel = s2_logits_rel.squeeze().softmax(0)
    	    s2_preds_hb = s2_logits_hb.squeeze().softmax(0)
    	    s1_preds_np = s1_preds[1, :].detach().cpu().numpy()
    	    s2_preds_rel_np = s2_preds_rel[1, :].detach().cpu().numpy()
    	    s2_preds_hb_np = s2_preds_hb[1, :].detach().cpu().numpy()

    	    selected_ids = selected_ids.squeeze().detach().cpu().numpy()
    	    if tt != 2:
    	        s2_preds_np = s2_preds_hb_np

    	        pos_edge_ids = np.where(s2_preds_np >= 0.9)
    	        neg_edge_ids = np.where(s2_preds_np <= 0.01)
    	        for pos_id in pos_edge_ids[0]:
    	            actual_id = selected_ids[pos_id]
    	            if gt_values[0, actual_id] != 2:
    	                continue
    	            all_pos_ids.add(actual_id)
    	            all_edge_confs[actual_id] = s2_preds_np[pos_id]
    	            gt_values[0, actual_id] = 1
    	        for neg_id in neg_edge_ids[0]:
    	            actual_id = selected_ids[neg_id]
    	            if gt_values[0, actual_id] != 2:
    	                continue
    	            gt_values[0, actual_id] = 0
    	        num_to_pred = (gt_values == 2).sum()
    	        if num_to_pred <= num_filtered:
    	            break
    	    else:
    	        s2_preds_np = s2_preds_hb_np

    	        pos_edge_ids = np.where(s2_preds_np >= 0.5)
    	        for pos_id in pos_edge_ids[0]:
    	            actual_id = selected_ids[pos_id]
    	            if s2_mask[0][pos_id] is True or gt_values[0, actual_id] != 2:
    	                continue
    	            all_pos_ids.add(actual_id)
    	            all_edge_confs[actual_id] = s2_preds_np[pos_id]

        pos_edge_ids = list(all_pos_ids)
        edge_confs = [all_edge_confs[idx] for idx in pos_edge_ids]
        pos_edges = edge_ids[pos_edge_ids].cpu().numpy()
        edge_confs = np.array(edge_confs)

        pred_corners, pred_confs, pos_edges = postprocess_preds(pred_corners, pred_confs, pos_edges)
        pred_data = {
            'corners': pred_corners,
            'edges': pos_edges,
        }

        return pred_data

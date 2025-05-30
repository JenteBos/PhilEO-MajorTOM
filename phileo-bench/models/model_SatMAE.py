from functools import partial

import torch
import torch.nn as nn
from model.model_DecoderUtils import CoreDecoder, EncoderBlock

from timm.models.vision_transformer import PatchEmbed, Block
import timm
from collections import OrderedDict
from utils.training_utils import get_2d_sincos_pos_embed, get_1d_sincos_pos_embed_from_grid
from model.model_CoreCNN import CoreCNNBlock, get_activation

class ViTGroupedChannelsEncoder(nn.Module):
    """ 
        VisionTransformer backbone
    """

    def __init__(self, img_size=96, patch_size=8, in_chans=10, output_dim=1,
                 channel_groups=((0, 1, 2, 3), (4, 5, 6, 7), (8, 9)),
                 # order S2 bands: 0-B02, 1-B03, 2-B04, 3-B08, 4-B05, 5-B06, 6-B07, 7-B8A, 8-B11, 9-B12
                 # groups: (i) RGB+NIR - B2, B3, B4, B8 (ii) Red Edge - B5, B6, B7, B8A (iii) SWIR - B11, B12,
                 channel_embed=256, embed_dim=1024, depth=24, num_heads=16,
                 mlp_ratio=4., norm_layer=nn.LayerNorm
                 ):
        
        super().__init__()
        
        # Attributes
        self.in_c = in_chans
        self.patch_size = patch_size
        self.channel_groups = channel_groups
        self.output_dim = output_dim
        num_groups = len(channel_groups)

        # --------------------------------------------------------------------------
        # MAE encoder specifics
        self.patch_embed = nn.ModuleList([PatchEmbed(img_size, patch_size, len(group), embed_dim)
                                          for group in channel_groups])
        # self.patch_embed = PatchEmbed(img_size, patch_size, 1, embed_dim)
        self.num_patches = self.patch_embed[0].num_patches

        self.cls_token = nn.Parameter(torch.zeros(1, 1, embed_dim), requires_grad=True)
        self.pos_embed = nn.Parameter(torch.zeros(1, self.num_patches + 1, embed_dim - channel_embed),
                                      requires_grad=False)  # fixed sin-cos embedding
        self.channel_embed = nn.Parameter(torch.zeros(1, num_groups, channel_embed), requires_grad=False)
        # self.enc_mask_token = nn.Parameter(torch.zeros(1, 1, embed_dim))

        self.blocks = nn.ModuleList([
            Block(embed_dim, num_heads, mlp_ratio, qkv_bias=True, norm_layer=norm_layer)
            for i in range(depth)])
        self.norm = norm_layer(embed_dim)

        
        self.initialize_weights()
        # --------------------------------------------------------------------------

    def initialize_weights(self):
        # initialization
        # initialize (and freeze) pos_embed by sin-cos embedding
        pos_embed = get_2d_sincos_pos_embed(self.pos_embed.shape[-1], int(self.num_patches ** .5),
                                            cls_token=True)
        self.pos_embed.data.copy_(torch.from_numpy(pos_embed).float().unsqueeze(0))


        # initialize patch_embed like nn.Linear (instead of nn.Conv2d)
        w = self.patch_embed[0].proj.weight.data
        torch.nn.init.xavier_uniform_(w.view([w.shape[0], -1]))

        # timm's trunc_normal_(std=.02) is effectively normal_(std=0.02) as cutoff is too big (2.)
        torch.nn.init.normal_(self.cls_token, std=.02)

        # initialize nn.Linear and nn.LayerNorm
        self.apply(self._init_weights)

    def _init_weights(self, m):
        if isinstance(m, nn.Linear):
            # we use xavier_uniform following official JAX ViT:
            torch.nn.init.xavier_uniform_(m.weight)
            if isinstance(m, nn.Linear) and m.bias is not None:
                nn.init.constant_(m.bias, 0)
        elif isinstance(m, nn.LayerNorm):
            nn.init.constant_(m.bias, 0)
            nn.init.constant_(m.weight, 1.0)

    def forward(self, x):
        # x is (N, C, H, W)
        b, c, h, w = x.shape

        x_c_embed = []
        for i, group in enumerate(self.channel_groups):
            x_c = x[:, group, :, :]
            x_c_embed.append(self.patch_embed[i](x_c))  # (N, L, D)

        x = torch.stack(x_c_embed, dim=1)  # (N, G, L, D)
        _, G, L, D = x.shape

        # add channel embed
        channel_embed = self.channel_embed.unsqueeze(2)  # (1, G, 1, cD)
        pos_embed = self.pos_embed[:, 1:, :].unsqueeze(1)  # (1, 1, L, pD)

        # Channel embed same across (x,y) position, and pos embed same across channel (c)
        channel_embed = channel_embed.expand(-1, -1, pos_embed.shape[2], -1)  # (1, G, L, cD)
        pos_embed = pos_embed.expand(-1, channel_embed.shape[1], -1, -1)  # (1, G, L, pD)
        pos_channel = torch.cat((pos_embed, channel_embed), dim=-1)  # (1, G, L, D)

        # add pos embed w/o cls token
        x = x + pos_channel  # (N, G, L, D)
        x = x.view(b, -1, D) # (N, L, D)

        # append cls token
        cls_tokens = self.cls_token.expand(x.shape[0], -1, -1)
        x = torch.cat((cls_tokens, x), dim=1)  # (N, G*L + 1, D)

        # apply Transformer blocks
        for blk in self.blocks:
            x = blk(x)
        x = self.norm(x)

        # # remove cls token
        # x = x[:, 1:, :]

        return x


class SatMAE(nn.Module):
    """ Masked Autoencoder with VisionTransformer backbone
    """

    def __init__(self, img_size=96, patch_size=8, in_chans=10, output_dim=1,
                 channel_groups=((0, 1, 2, 3), (4, 5, 6, 7), (8, 9)),
                 # order S2 bands: 0-B02, 1-B03, 2-B04, 3-B08, 4-B05, 5-B06, 6-B07, 7-B8A, 8-B11, 9-B12
                 # groups: (i) RGB+NIR - B2, B3, B4, B8 (ii) Red Edge - B5, B6, B7, B8A (iii) SWIR - B11, B12,
                 channel_embed=256, embed_dim=1024, depth=24, num_heads=16,
                 mlp_ratio=4., norm_layer=nn.LayerNorm, norm_pix_loss=False,
                 decoder_norm='batch', decoder_padding='same',
                 decoder_activation='relu', decoder_depths=[2, 2, 8, 2], decoder_dims=[160, 320, 640, 1280]
                 ):
        super().__init__()

        self.in_c = in_chans
        self.patch_size = patch_size
        self.channel_groups = channel_groups
        self.output_dim = output_dim
        num_groups = len(channel_groups)

        # --------------------------------------------------------------------------
        # encoder specifics
        self.vit_encoder = ViTGroupedChannelsEncoder(img_size=img_size, patch_size=patch_size, 
                                                     in_chans=in_chans, output_dim=output_dim,
                                                     channel_groups=channel_groups,
                                                     channel_embed=channel_embed, embed_dim=embed_dim, depth=depth, num_heads=num_heads,
                                                     mlp_ratio=mlp_ratio, norm_layer=norm_layer,)
 
        # --------------------------------------------------------------------------

        # --------------------------------------------------------------------------


        # CNN Decoder Blocks:
        self.depths = decoder_depths
        self.dims = decoder_dims

        self.decoder_head = CoreDecoder(embedding_dim=embed_dim*3,
                                        output_dim=output_dim,
                                        depths=decoder_depths, 
                                        dims= decoder_dims,
                                        activation=decoder_activation,
                                        padding=decoder_padding, 
                                        norm=decoder_norm)


        self.decoder_downsample_block = nn.Sequential(EncoderBlock(depth=1, in_channels=embed_dim*3,
                                                                   out_channels=embed_dim*3, norm=decoder_norm, activation=decoder_activation,
                                                                   padding=decoder_padding))




    def reshape(self, x):
        # Separate channel axis
        N, GL, D = x.shape
        G = len(self.channel_groups)
        x = x.view(N, G, GL//G, D)

        # predictor projection
        x_c_patch = []
        for i, group in enumerate(self.channel_groups):
            x_c = x[:, i].permute(0, 2, 1)  # (N, D, L)
            x_c = x_c.view(x_c.shape[0], x_c.shape[1], int(x_c.shape[2] ** 0.5), int(x_c.shape[2] ** 0.5))
            x_c_patch.append(x_c)

        x = torch.cat(x_c_patch, dim=1)
        return x

    def forward(self, x):
        x = self.vit_encoder(x)

        # remove cls token
        x = x[:, 1:, :]
        # reshape into 2d features
        x = self.reshape(x)
        x = self.decoder_downsample_block(x)
        x = self.decoder_head(x)
        return x


def vit_base(**kwargs):
    model = SatMAE(
        channel_embed=256, embed_dim=768, depth=12, num_heads=12, mlp_ratio=4,
        norm_layer=partial(nn.LayerNorm, eps=1e-6), **kwargs)
    return model


def vit_large(**kwargs):
    model = SatMAE(
        channel_embed=256, embed_dim=1024, depth=24, num_heads=16, mlp_ratio=4,
        norm_layer=partial(nn.LayerNorm, eps=1e-6), **kwargs)
    return model


def vit_huge(**kwargs):
    model = SatMAE(
        embed_dim=1280, depth=32, num_heads=16, mlp_ratio=4,
        norm_layer=partial(nn.LayerNorm, eps=1e-6), **kwargs)
    return model


def satmae_vit_cnn(checkpoint, img_size=96, patch_size=8, in_chans=10, output_dim=1,
                   decoder_norm='batch', decoder_padding='same', decoder_activation='relu', decoder_depths=[2, 2, 8, 2],
                   decoder_dims=[160, 320, 640, 1280], freeze_body=True, classifier=False, **kwargs):


    model = vit_large(img_size=img_size, patch_size=patch_size, in_chans=in_chans, output_dim=output_dim,
                          decoder_norm=decoder_norm, decoder_padding=decoder_padding, decoder_activation=decoder_activation,
                          decoder_depths=decoder_depths, decoder_dims=decoder_dims,
                          **kwargs)

    # load pre-trained model weights
    state_dict = model.vit_encoder.state_dict()
    checkpoint_model = checkpoint['model']

    # model_sd, shared_weights = load_encoder_weights(checkpoint_model, state_dict)

    for k in ['pos_embed', 'patch_embed.proj.weight', 'patch_embed.proj.bias', 'head.weight', 'head.bias']:
        if k in checkpoint_model and checkpoint_model[k].shape != state_dict[k].shape:
            print(f"Removing key {k} from pretrained checkpoint")
            del checkpoint_model[k]

    # load pre-trained model
    msg = model.vit_encoder.load_state_dict(checkpoint_model, strict=False)
    print(msg)

    if freeze_body:
        for _, param in model.vit_encoder.named_parameters():
            param.requires_grad = False

    return model


if __name__ == '__main__':
    checkpoint = torch.load('/phileo_data/pretrained_models/pretrain-vit-large-e199.pth')
    checkpoint_model = checkpoint['model']

    model = vit_large(img_size=96, patch_size=8, in_chans=10)
    state_dict = model.state_dict()

    # model_sd, shared_weights = load_encoder_weights(checkpoint_model, state_dict)

    for k in ['pos_embed', 'patch_embed.proj.weight', 'patch_embed.proj.bias', 'head.weight', 'head.bias']:
        if k in checkpoint_model and checkpoint_model[k].shape != state_dict[k].shape:
            print(f"Removing key {k} from pretrained checkpoint")
            del checkpoint_model[k]


    # load pre-trained model
    msg = model.load_state_dict(checkpoint_model, strict=False)
    print(msg)

    x = model(torch.randn((4, 10, 96, 96)))


    print()

    print('help')
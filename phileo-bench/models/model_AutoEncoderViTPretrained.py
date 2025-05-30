from model.model_SatMAE import SatMAE
from model.model_DecoderUtils import CoreDecoder, EncoderBlock
import torch.nn as nn
import torch
from functools import partial
from collections import OrderedDict
from timm.models.vision_transformer import PatchEmbed, Block
from utils.training_utils import get_2d_sincos_pos_embed, get_1d_sincos_pos_embed_from_grid

class ViTEncoder(nn.Module):
    """ 
        VisionTransformer backbone
    """

    def __init__(self, chw:tuple=(10, 64, 64), patch_size:int=4, output_dim:int=10,
                 embed_dim=768, depth=12, num_heads=16, mlp_ratio=4., norm_layer=nn.LayerNorm, 
                 ):
        
        super().__init__()

        # Attributes
        self.chw = chw  # (C, H, W)
        self.in_c = chw[0]
        self.img_size = chw[1]
        self.patch_size = patch_size
        self.output_dim = output_dim

        # --------------------------------------------------------------------------
        # MAE encoder specifics
        self.patch_embed = PatchEmbed(self.img_size, patch_size, self.in_c, embed_dim)
        num_patches = self.patch_embed.num_patches

        self.cls_token = nn.Parameter(torch.zeros(1, 1, embed_dim))
        self.pos_embed = nn.Parameter(torch.zeros(1, num_patches + 1, embed_dim),
                                      requires_grad=False)  # learnable with sin-cos embedding init

        self.blocks = nn.ModuleList([
            Block(embed_dim, num_heads, mlp_ratio, qkv_bias=True, norm_layer=norm_layer)
            for i in range(depth)])
        self.norm = norm_layer(embed_dim)

        
        self.initialize_weights()
        # --------------------------------------------------------------------------

    def initialize_weights(self):
        # initialization
        # initialize (and freeze) pos_embed by sin-cos embedding
        pos_embed = get_2d_sincos_pos_embed(self.pos_embed.shape[-1], int(self.patch_embed.num_patches ** .5),
                                            cls_token=True)
        self.pos_embed.data.copy_(torch.from_numpy(pos_embed).float().unsqueeze(0))

        # initialize patch_embed like nn.Linear (instead of nn.Conv2d)
        w = self.patch_embed.proj.weight.data
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
        # embed patches
        x = self.patch_embed(x)

        # add pos embed w/o cls token
        x = x + self.pos_embed[:, 1:, :]

        # append cls token
        cls_token = self.cls_token + self.pos_embed[:, :1, :]
        cls_tokens = cls_token.expand(x.shape[0], -1, -1)
        x = torch.cat((cls_tokens, x), dim=1)

        # apply Transformer blocks
        for blk in self.blocks:
            x = blk(x)
        x = self.norm(x)
        # # remove cls token
        # x = x[:, 1:, :]

        return x


class ViTCNN(nn.Module):
    """ 
    Autoencoder model with pretrained VisionTransformer backbone
    """

    def __init__(self, chw:tuple=(10, 64, 64), patch_size:int=4, output_dim:int=10,
                 embed_dim=768, depth=12, num_heads=16,
                 mlp_ratio=4., norm_layer=nn.LayerNorm, norm_pix_loss=False, noisy_mask_token=True,
                 decoder_norm='batch', decoder_padding='same',
                 decoder_activation='relu', decoder_depths=[2, 2, 8, 2], decoder_dims=[160, 320, 640, 1280]
                 ):
        super().__init__()

        # Attributes
        self.chw = chw  # (C, H, W)
        self.in_c = chw[0]
        self.img_size = chw[1]
        self.patch_size = patch_size
        self.noisy_mask_token = noisy_mask_token
        self.output_dim = output_dim

        # --------------------------------------------------------------------------
        # encoder specifics
        self.vit_encoder = ViTEncoder(chw=chw, 
                                      patch_size=patch_size, output_dim=output_dim,
                                      embed_dim=embed_dim, depth=depth, num_heads=num_heads,
                                      mlp_ratio=mlp_ratio, norm_layer=norm_layer)
 
        # --------------------------------------------------------------------------

        # --------------------------------------------------------------------------
        # CNN Decoder Blocks:
        self.depths = decoder_depths
        self.dims = decoder_dims
        self.decoder_head = CoreDecoder(embedding_dim=embed_dim,
                                        output_dim=output_dim,
                                        depths=decoder_depths, 
                                        dims= decoder_dims,
                                        activation=decoder_activation,
                                        padding=decoder_padding, 
                                        norm=decoder_norm)
        

        self.decoder_downsample_block = nn.Sequential(EncoderBlock(depth=1, in_channels=embed_dim,
                                                                   out_channels=embed_dim, norm=decoder_norm, activation=decoder_activation,
                                                                   padding=decoder_padding),
                                                      EncoderBlock(depth=1, in_channels=embed_dim,
                                                                   out_channels=embed_dim, norm=decoder_norm, activation=decoder_activation, 
                                                                   padding=decoder_padding)
                                                      )

        # --------------------------------------------------------------------------   

    def reshape(self, x):
        # Separate channel axis
        N, L, D = x.shape
        x = x.permute(0, 2, 1)
        x = x.view(N, D, int(L ** 0.5), int(L ** 0.5))

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


class ViTCNN_gc(SatMAE):

    def __init__(self, **kwargs):
        super(ViTCNN_gc, self).__init__(**kwargs)

        embedding_dim = int(kwargs['embed_dim']*3)
        self.decoder_downsample_block = nn.Sequential(EncoderBlock(depth=1, in_channels=embedding_dim,
                                                                   out_channels=embedding_dim, norm='batch',
                                                                       activation='relu', padding='same'),
                                                      EncoderBlock(depth=1, in_channels=embedding_dim,
                                                                   out_channels=embedding_dim, norm='batch',
                                                                   activation='relu', padding='same')
                                                      )


def vit_base_gc(**kwargs):
    model = ViTCNN_gc(
        channel_embed=256, embed_dim=768, depth=12, num_heads=12, mlp_ratio=4,
        norm_layer=partial(nn.LayerNorm, eps=1e-6), **kwargs)
    return model


def vit_large_gc(**kwargs):
    model = ViTCNN_gc(
        channel_embed=256, embed_dim=1024, depth=24, num_heads=16, mlp_ratio=4,
        norm_layer=partial(nn.LayerNorm, eps=1e-6), **kwargs)
    return model


def vit_huge_gc(**kwargs):
    model = ViTCNN_gc(
        embed_dim=1280, depth=32, num_heads=16, mlp_ratio=4,
        norm_layer=partial(nn.LayerNorm, eps=1e-6), **kwargs)
    return model


def vit_base(**kwargs):
    model = ViTCNN(embed_dim=768, depth=12, num_heads=12, mlp_ratio=4,
                   norm_layer=partial(nn.LayerNorm, eps=1e-6), **kwargs)
    return model


def vit_large(**kwargs):
    model = ViTCNN(embed_dim=1024, depth=24, num_heads=16, mlp_ratio=4,
                   norm_layer=partial(nn.LayerNorm, eps=1e-6), **kwargs)
    return model


def vit_huge(**kwargs):
    model = ViTCNN(embed_dim=1280, depth=32, num_heads=16, mlp_ratio=4,
                   norm_layer=partial(nn.LayerNorm, eps=1e-6), **kwargs)
    return model


def vit_cnn_gc(checkpoint, img_size=128, patch_size=4, in_chans=10, output_dim=1, freeze_body=True, **kwargs):

    model = vit_base_gc(img_size=img_size, patch_size=patch_size, in_chans=in_chans, output_dim=output_dim,  **kwargs)
    state_dict = model.vit_encoder.state_dict()

    for k in ['pos_embed', 'patch_embed.proj.weight', 'patch_embed.proj.bias', 'head.weight', 'head.bias']:
        if k in checkpoint and checkpoint[k].shape != state_dict[k].shape:
            print(f"Removing key {k} from pretrained checkpoint")
            del checkpoint[k]

    # load pre-trained model weights
    msg = model.vit_encoder.load_state_dict(checkpoint, strict=False)
    print(msg)

    if freeze_body:
        for _, param in model.vit_encoder.named_parameters():
            param.requires_grad = False

    return model


def vit_cnn(checkpoint, img_size=128, patch_size=4, in_chans=10, output_dim=1, freeze_body=True, **kwargs):

    model = vit_large(chw=(in_chans, img_size, img_size), patch_size=patch_size, output_dim=output_dim,  **kwargs)
    state_dict = model.vit_encoder.state_dict()

    for k in ['pos_embed', 'patch_embed.proj.weight', 'patch_embed.proj.bias', 'head.weight', 'head.bias']:
        if k in checkpoint and checkpoint[k].shape != state_dict[k].shape:
            print(f"Removing key {k} from pretrained checkpoint")
            del checkpoint[k]

    # load pre-trained model weights
    msg = model.vit_encoder.load_state_dict(checkpoint, strict=False)
    print(msg)

    if freeze_body:
        for _, param in model.vit_encoder.named_parameters():
            param.requires_grad = False

    return model


def get_core_decoder_kwargs(output_dim, core_size, full_unet=True, **kwargs):
    core_kwargs = {'output_dim': output_dim, 'decoder_norm': 'batch', 'decoder_padding': 'same',
                   'decoder_activation': 'relu'}

    if core_size == 'core_nano':
        core_kwargs['decoder_depths'] = [2, 2, 8, 2]
        core_kwargs['decoder_dims'] = [80, 160, 320, 640]

    elif core_size == 'core_tiny':
        core_kwargs['decoder_depths'] = [3, 3, 9, 3]
        core_kwargs['decoder_dims'] = [96, 192, 384, 768]

    elif core_size == 'core_base':
        core_kwargs['decoder_depths'] = [3, 3, 27, 3]
        core_kwargs['decoder_dims'] = [128, 256, 512, 1024]

    else:
        raise ValueError

    core_kwargs.update(kwargs)
    return core_kwargs


if __name__ == '__main__':
    sd = torch.load('/phileo_data/pretrained_models/31102023_MaskedAutoencoderViT/MaskedAutoencoderViT_ckpt.pt', map_location='cpu')
    model = vit_cnn(checkpoint=sd)
    x = model(torch.randn((4, 10, 128, 128)))
    print()
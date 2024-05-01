import torch
import torch.nn as nn
from lobotomy.models.litgpt.config import Config
from typing import Optional
import math
from lobotomy.models.litgpt.modules.kv_cache import KVCache
from lobotomy.models.litgpt.super_layers.linear_super import SuperLinear

class CausalSelfAttention(nn.Module):
    def __init__(self, config: Config, rotary_emb: nn.Module) -> None:
        super().__init__()
        shape = (config.n_head + 2 * config.n_query_groups) * config.head_size
        # key, query, value projections for all heads, but in a batch
        self.attn = SuperLinear(config.n_embd, shape, bias=config.bias)
        # output projection
        # if `head_size` is explicitly specified in the config, `n_emd` might not be equal to `head_size * n_head`
        self.proj = SuperLinear(config.head_size * config.n_head, config.n_embd, bias=config.bias)
        # disabled by default
        self.kv_cache: Optional[KVCache] = None
        self.rotary_embedding = rotary_emb
        self.config = config
        self.sample_embed_dim = None # type: Optional[int]
        self.sample_n_head = None # type: Optional[int]
        self.sample_head_size = None # type: Optional[int]
        self.sample_qkv_shape = None # type: Optional[int]
        self.device = config.device

    def set_sample_config(self, sample_embed_dim:int, sample_n_head:int) -> None:
        self.sample_embed_dim = sample_embed_dim
        self.sample_n_head = sample_n_head
        if self.config.n_query_groups == 1:
            self.sample_n_query_groups = 1
        else:
            self.sample_n_query_groups = self.sample_n_head // (self.config.n_head//self.config.n_query_groups)
        if self.config.fix_head_size:
           self.sample_head_size = self.config.head_size
           self.sample_qkv_shape = (self.sample_n_head + 2 * self.sample_n_query_groups) * self.sample_head_size
        else:
           self.sample_head_size = self.config.n_embd// self.sample_n_head
           self.sample_qkv_shape = (self.config.n_head + 2 * self.config.n_query_groups) * self.config.head_size
        
        #print(self.sample_qkv_shape)
        self.attn.set_sample_config(sample_embed_dim, self.sample_qkv_shape)
        self.proj.set_sample_config(self.sample_head_size*self.sample_n_head, sample_embed_dim)
        self.rotary_embedding.set_sample_config(self.config.n_embd, sample_n_head)
        self.cos, self.sin = self.reset_parameters(device=self.device)

    def reset_parameters(self, device="cuda") -> None:
        # Trigger resetting the rope-cache
        cos, sin = self.rotary_embedding.rope_cache(device=device)
        return cos, sin


    def forward(
        self,
        x: torch.Tensor,
        mask: Optional[torch.Tensor] = None,
        input_pos: Optional[torch.Tensor] = None,
    ) -> torch.Tensor:
        B, T, C = x.size()  # batch size, sequence length, embedding dimensionality (n_embd)

        qkv = self.attn(x)
        cos = self.cos[:T]
        sin = self.sin[:T]

        # assemble into a number of query groups to support MHA, MQA and GQA together (see `config.n_query_groups`)
        if self.config.fix_head_size:
            q_per_kv = self.sample_n_head // self.sample_n_query_groups
        else:
            q_per_kv = self.config.n_head // self.config.n_query_groups
        total_qkv = q_per_kv + 2  # each group has 1+ queries, 1 key, and 1 value
        if self.config.fix_head_size:
            qkv = qkv.view(B, T, self.sample_n_query_groups, total_qkv, self.sample_head_size)
        else:
            qkv = qkv.view(B, T, self.config.n_query_groups, total_qkv, self.config.head_size)
        qkv = qkv.permute(0, 2, 3, 1, 4)  # (B, n_query_groups, total_qkv, T, hs)

        # split batched computation into three
        q, k, v = qkv.split((q_per_kv, 1, 1), dim=2)

        # maybe repeat k and v if for the non multi-head attention cases
        # training: flash attention requires it
        # inference: multi-query would require a full kv cache so avoid it to limit its memory usage
        if self.config.fix_head_size:
          if self.sample_n_query_groups != self.sample_n_head and (input_pos is None or self.config.n_query_groups != 1):          
            k = k.expand(B, self.sample_n_query_groups, q_per_kv, T, self.sample_head_size)
            v = v.expand(B, self.sample_n_query_groups, q_per_kv, T, self.sample_head_size)
        else:
          if self.config.n_query_groups != self.config.n_head and (input_pos is None or self.config.n_query_groups != 1):
            k = k.expand(B, self.config.n_query_groups, q_per_kv, T, self.config.head_size)
            v = v.expand(B, self.config.n_query_groups, q_per_kv, T, self.config.head_size)

        if self.config.fix_head_size:
            q = q.reshape(B, -1, T, self.sample_head_size)
            k = k.reshape(B, -1, T, self.sample_head_size)
            v = v.reshape(B, -1, T, self.sample_head_size)
        else:
            sample_q_per_kv = self.sample_n_head // self.sample_n_query_groups
            #print(q.shape)
            q = q[:,:self.sample_n_query_groups,:sample_q_per_kv,:,:]
            q = q.reshape(B, -1, T, self.config.head_size)  # (B, nh_q, T, hs)
            k = k[:,:self.sample_n_query_groups,:,:,:]
            k = k.reshape(B, -1, T, self.config.head_size)  # (B, nh_k, T, hs)
            v = v[:,:self.sample_n_query_groups,:,:,:]
            v = v.reshape(B, -1, T, self.config.head_size)  # (B, nh_v, T, hs)
            v = torch.nn.functional.pad(v,(0,abs(self.config.head_size-self.sample_head_size)))
            #print(q.shape)
            #print(k.shape)
            #print(v.shape)
        if self.config.fix_head_size:
            rope_n_elem = int(self.sample_head_size * self.config.rotary_percentage)
        else:
            #print(self.config.head_size)
            rope_n_elem = int(self.config.head_size * self.config.rotary_percentage)
        q_roped = self.rotary_embedding.apply_rope(q[..., :rope_n_elem], cos, sin)
        k_roped = self.rotary_embedding.apply_rope(k[..., :rope_n_elem], cos, sin)
        q = torch.cat((q_roped, q[..., rope_n_elem :]), dim=-1)
        k = torch.cat((k_roped, k[..., rope_n_elem :]), dim=-1)

        if input_pos is not None:
            if not isinstance(self.kv_cache, KVCache):
                raise TypeError("You need to call `gpt.set_kv_cache()`")
            k, v = self.kv_cache(input_pos, k, v)

        y = self.scaled_dot_product_attention(q, k, v, mask)
        #print(y.shape)
        #print(self.sample_n_head)
        #print(self.sample_head_size)
        y = y.reshape(B, T, self.sample_head_size * self.sample_n_head)  # re-assemble all head outputs side by side
        #print("Y",y.shape)
        # output projection
        return self.proj(y)

    def scaled_dot_product_attention(
        self, q: torch.Tensor, k: torch.Tensor, v: torch.Tensor, mask: Optional[torch.Tensor] = None
    ) -> torch.Tensor:
        scale = 1.0 / math.sqrt(self.config.head_size)
        y = torch.nn.functional.scaled_dot_product_attention(
            q, k, v, attn_mask=mask, dropout_p=0.0, scale=scale, is_causal=mask is None
        )
        return y.transpose(1, 2)

    def build_kv_cache(
        self,
        batch_size: int,
        max_seq_length: int,
        rope_cache_length: Optional[int] = None,
        device: Optional[torch.device] = None,
        dtype: Optional[torch.dtype] = None,
    ) -> "KVCache":
        heads = 1 if self.config.n_query_groups == 1 else self.config.n_head
        v_shape = (batch_size, heads, max_seq_length, self.config.head_size)
        if rope_cache_length is None:
            if self.config.rotary_percentage != 1.0:
                raise TypeError("Please pass the `rope_cache_length=gpt.cos.size(-1)` value")
            k_shape = v_shape
        else:
            k_shape = (
                batch_size,
                heads,
                max_seq_length,
                rope_cache_length + self.config.head_size - self.config.rope_n_elem,
            )
        return KVCache(k_shape, v_shape, device=device, dtype=dtype)
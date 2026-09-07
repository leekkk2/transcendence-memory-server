"""Bound gateway inputs without discarding long memory content.

The length-weighted mean is normalized only for multi-part inputs. Both sync
worker and async search use this exact transform; enabling/changing the cap
requires rebuilding affected indexes. Limits default off for compatibility.
"""
import os
import numpy as np


def limits():
    return (max(0, int(os.environ.get('TM_EMBEDDING_MAX_INPUT_CHARS', '0'))),
            max(0, int(os.environ.get('TM_EMBEDDING_MAX_BATCH_SIZE', '0'))))


def parts(text, cap):
    return [text[i:i+cap] for i in range(0, len(text), cap)] if cap and len(text)>cap else [text]


def pool(vectors, texts):
    values=np.asarray(vectors, dtype=np.float32)
    if len(texts)==1:
        return values[0]
    mean=np.average(values, axis=0, weights=[max(1,len(t)) for t in texts])
    norm=np.linalg.norm(mean)
    if not np.isfinite(mean).all() or norm==0:
        raise ValueError('Cannot aggregate non-finite or zero embedding vectors')
    return np.asarray(mean/norm,dtype=np.float32)


async def embed_bounded(texts, call):
    cap, batch_size=limits()
    groups=[parts(text,cap) for text in texts]
    flat=[part for group in groups for part in group]
    vectors=[]
    for i in range(0,len(flat),batch_size or max(1,len(flat))):
        vectors.extend(await call(flat[i:i+(batch_size or len(flat))]))
    if len(vectors)!=len(flat):
        raise ValueError('Embedding response count does not match input count')
    result=[]
    offset=0
    for group in groups:
        result.append(pool(vectors[offset:offset+len(group)],group))
        offset+=len(group)
    return np.asarray(result,dtype=np.float32)

from __future__ import annotations

import json
import numpy as np
import pandas as pd
import torch

from aegis.data import synthetic
from aegis import kronos_adapter as ka


def test_kronos_direct_upstream_parity_and_forward_calendar():
    st = ka.status()
    assert st['state'] == 'READY', st

    ds = synthetic()
    index = 59
    horizon = 4
    seed = 26

    # Adapter call
    res = ka.forecast(ds, index, 'NVDA', horizon=horizon, samples=2, seed=seed)
    assert res['status'] == 'REAL_PRETRAINED_KRONOS'
    assert len(res['paths']) == 2

    # Direct upstream predictor call with identical inputs and seeds
    predictor, m = ka.load()
    bars = ds.prefix('NVDA', index)[-m['max_context']:]
    frame = pd.DataFrame([{k: b[k] for k in ('open', 'high', 'low', 'close', 'volume')} for b in bars])
    x = pd.Series(pd.to_datetime([b['timestamp'] for b in bars], utc=True))
    y = pd.Series(pd.to_datetime([ds.row('NVDA', i)['timestamp'] for i in range(index + 1, index + horizon + 1)], utc=True))

    direct_paths = []
    with torch.random.fork_rng(devices=list(range(torch.cuda.device_count())) if torch.cuda.is_available() else []):
        with torch.inference_mode():
            for j in range(2):
                torch.manual_seed(seed + j)
                p = predictor.predict(frame, x, y, pred_len=horizon, T=1.0, top_k=0, top_p=0.9, sample_count=1, verbose=False)
                direct_paths.append(p['close'].to_numpy(dtype=float).tolist())

    np.testing.assert_allclose(np.array(res['paths']), np.array(direct_paths), rtol=1e-5, atol=1e-5)

    # Forward exchange-calendar inference at end of finite dataset
    end_res = ka.forecast(ds, ds.length - 1, 'NVDA', horizon=4, samples=2, seed=seed, use_exchange_calendar=True)
    assert end_res['status'] == 'REAL_PRETRAINED_KRONOS'
    assert len(end_res['bands']) == 4

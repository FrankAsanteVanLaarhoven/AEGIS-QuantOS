"""Interface tests only; explicit predictor doubles are NOT pretrained validation."""
import pytest
np=pytest.importorskip('numpy');pd=pytest.importorskip('pandas');torch=pytest.importorskip('torch')
from aegis.data import synthetic
from aegis import kronos_adapter as k

class PredictorDouble:
    def __init__(self,invalid=False):self.calls=[];self.invalid=invalid
    def predict(self,df,x,y,**kwargs):
        self.calls.append((df.copy(),x.copy(),y.copy(),kwargs));n=len(y);close=100+torch.rand(n).numpy()
        return pd.DataFrame({'open':close,'high':close+1 if not self.invalid else close-1,'low':close-1,'close':close,'volume':np.ones(n)*100,'amount':np.ones(n)*10000},index=y)

@pytest.fixture
def installed_double(monkeypatch):
    pred=PredictorDouble();m={'max_context':512,'model_repo':'EXPLICIT_TEST_DOUBLE_NOT_PRETRAINED','model_revision':'test','tokenizer_revision':'test'}
    monkeypatch.setattr(k,'load',lambda:(pred,m));return pred

def test_independent_paths_not_upstream_averages(installed_double):
    r=k.forecast(synthetic(),59,'NVDA',horizon=3,samples=4);assert len(r['paths'])==4
    assert all(c[3]['sample_count']==1 for c in installed_double.calls)
    assert len({tuple(p) for p in r['paths']})>1

def test_no_future_prices_in_predictor_input(installed_double):
    d=synthetic();k.forecast(d,59,'NVDA',horizon=3,samples=2)
    for frame,x,y,kwargs in installed_double.calls:
        assert len(frame)==60;assert frame.iloc[-1]['close']==d.row('NVDA',59)['close'];assert y.min()>x.max()

def test_seed_preserves_repeatable_paths(installed_double):
    d=synthetic();a=k.forecast(d,59,'NVDA',horizon=3,samples=2);b=k.forecast(d,59,'NVDA',horizon=3,samples=2);assert a['paths']==b['paths']

def test_invalid_ohlc_is_flagged_not_repaired(installed_double):
    installed_double.invalid=True;r=k.forecast(synthetic(),59,'NVDA',horizon=3,samples=2);assert r['ohlcv_violations']==6;assert not r['execution_eligible']

def test_calendar_unavailable_blocks(installed_double):
    with pytest.raises(ValueError):k.forecast(synthetic(),355,'NVDA',horizon=10,samples=2)


def test_mini_context_not_accidentally_truncated_to_512(monkeypatch):
    pred=PredictorDouble();meta={'max_context':2048,'model_repo':'EXPLICIT_TEST_DOUBLE_NOT_PRETRAINED','model_revision':'test','tokenizer_revision':'test'}
    monkeypatch.setattr(k,'load',lambda:(pred,meta))
    result=k.forecast(synthetic(sessions=12),600,'NVDA',horizon=3,samples=2)
    assert all(len(c[0])==601 for c in pred.calls)
    assert result['ohlcv_valid'] and not result['execution_eligible']

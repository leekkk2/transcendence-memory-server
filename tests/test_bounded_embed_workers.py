import threading
import time
from scripts import task_rag_lancedb_ingest as ingest


def test_bounded_map_preserves_order_and_limit():
    lock=threading.Lock();active=0;peak=0
    def work(n):
        nonlocal active,peak
        with lock:active+=1;peak=max(peak,active)
        time.sleep(.02*(4-n%4))
        with lock:active-=1
        return n*n
    assert list(ingest.ordered_bounded_map(work,range(12),3))==[n*n for n in range(12)]
    assert 1<peak<=3


def test_bounded_map_closes_threads_on_failure():
    def work(n):
        if n==2:raise ValueError('synthetic failure')
        return n
    import pytest
    with pytest.raises(ValueError):list(ingest.ordered_bounded_map(work,range(8),2))

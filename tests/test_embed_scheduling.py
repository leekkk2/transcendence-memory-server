from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from scripts.job_queue import JobQueue


def test_concurrent_enqueues_share_one_pending_job(tmp_path):
    queue=JobQueue(tmp_path/'queue.db'); barrier=Barrier(12)
    def enqueue(_):
        barrier.wait()
        return queue.enqueue(op='embed',container='alpha')
    with ThreadPoolExecutor(max_workers=12) as pool:
        ids=list(pool.map(enqueue,range(12)))
    assert len(set(ids))==1,ids


def test_running_job_keeps_a_followup_for_new_writes(tmp_path):
    queue=JobQueue(tmp_path/'queue.db')
    first=queue.enqueue(op='embed',container='alpha');running=queue.claim_next()
    assert running.id==first
    second=queue.enqueue(op='embed',container='alpha')
    assert second!=first
    assert queue.enqueue(op='embed',container='alpha')==second

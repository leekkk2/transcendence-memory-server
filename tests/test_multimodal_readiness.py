from types import SimpleNamespace
import subprocess


def test_parser_failure_is_redacted_and_cached(monkeypatch):
    from scripts import multimodal_readiness as probe
    probe.reset_cache()
    calls = []
    def run(*args, **kwargs):
        calls.append(kwargs)
        return SimpleNamespace(returncode=1, stdout='', stderr='private-path api-key')
    monkeypatch.setattr(probe.subprocess, 'run', run)
    assert probe.parser_readiness() == (False, 'multimodal parser unavailable')
    assert probe.parser_readiness() == (False, 'multimodal parser unavailable')
    assert len(calls) == 1
    assert 0 < calls[0]['timeout'] <= 15


def test_parser_timeout_is_bounded(monkeypatch):
    from scripts import multimodal_readiness as probe
    probe.reset_cache()
    def run(*args, **kwargs):
        raise subprocess.TimeoutExpired(args[0], kwargs['timeout'])
    monkeypatch.setattr(probe.subprocess, 'run', run)
    assert probe.parser_readiness() == (False, 'multimodal parser unavailable')


def test_parser_success_and_configuration_change(monkeypatch):
    from scripts import multimodal_readiness as probe
    probe.reset_cache()
    calls = []
    def run(*args, **kwargs):
        calls.append(args)
        return SimpleNamespace(returncode=0, stdout='', stderr='')
    monkeypatch.setattr(probe.subprocess, 'run', run)
    assert probe.parser_readiness() == (True, '')
    monkeypatch.setenv('RAG_PARSER', 'docling')
    assert probe.parser_readiness() == (True, '')
    assert len(calls) == 2

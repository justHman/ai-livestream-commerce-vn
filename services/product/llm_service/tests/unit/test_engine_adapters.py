"""Unit tests for the optional inference-engine adapters.

Covers validation, request shaping and response mapping for llama.cpp, SGLang,
vLLM and HF-transformers backends. The heavy SDK imports are faked so the
adapter control flow is exercised without loading real models / GPUs.
"""

from __future__ import annotations

import sys
from types import ModuleType

import pytest

from llm.engines.base import LLMRequest


# --- helpers ---------------------------------------------------------------


class _FakeLlama:
    """Fake llama_cpp.Llama with deterministic chat completions."""

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.calls = []

    def create_chat_completion(self, messages, **kwargs):
        self.calls.append((messages, kwargs))
        if kwargs.get("stream"):
            return [
                {"choices": [{"delta": {"content": "hel"}}]},
                {"choices": [{"delta": {"content": "lo"}}]},
                {"choices": [{"delta": {}}]},
            ]
        return {
            "choices": [{"message": {"content": "hello world"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 3, "completion_tokens": 2},
        }


class _FakeSGL:
    """Fake sglang module."""

    class Engine:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

        def generate(self, prompts, sampling_params=None):
            return [{"text": "sgl hello", "meta_info": {"finish_reason": "stop"}}]

        def shutdown(self):
            self.kwargs["_shut"] = True


class _FakeVLLM:
    """Fake vllm module: LLM + SamplingParams + get_tokenizer."""

    class LLM:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

        def generate(self, prompts, sampling_params=None, use_tqdm=False):
            return [
                _FakeOutput(prompts, sampling_params),
            ]

    class SamplingParams:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

    @staticmethod
    def get_tokenizer(*args, **kwargs):
        return None


class _FakeOutput:
    def __init__(self, prompts, sp):
        self.prompt_token_ids = [1, 2, 3]
        self.outputs = [_FakeSequence("vllm hello")]


class _FakeSequence:
    def __init__(self, text):
        self.text = text
        self.finish_reason = "stop"
        self.token_ids = [7, 8]


class _FakeTensor:
    """Minimal tensor stand-in with the attributes the adapter touches."""

    def __init__(self, data):
        self.data = data
        self.shape = (1, len(data[0]))

    def __getitem__(self, k):
        if isinstance(k, tuple) and k[0] == 0:
            return _FakeTensor([self.data[k[1]]])
        return _FakeTensor([self.data[k]])

    def __len__(self):
        return len(self.data)


class _FakeTokenizer:
    """Fake HF tokenizer with a deterministic apply_chat_template."""

    eos_token_id = 2

    def apply_chat_template(self, messages, **kwargs):
        return " ".join(m["content"] for m in messages)

    def __call__(self, prompt, **kwargs):
        return {"input_ids": _FakeTensor([[1, 2, 3]])}

    def decode(self, ids, **kwargs):
        return "hf hello"


class _FakeCuda:
    @staticmethod
    def is_available():
        return False

    @staticmethod
    def empty_cache():
        pass


class _FakeTorch:
    cuda = _FakeCuda

    @staticmethod
    def is_available():
        return False

    @staticmethod
    def no_grad():
        class _Ctx:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        return _Ctx()

    class float16:
        pass

    class float32:
        pass

    class bfloat16:
        pass

    @staticmethod
    def empty_cache():
        pass


def _install_fake(monkeypatch, module_name, fake):
    mod = ModuleType(module_name)
    for name in dir(fake):
        if not name.startswith("_"):
            setattr(mod, name, getattr(fake, name))
    monkeypatch.setitem(sys.modules, module_name, mod)


def _install_llama_cpp(monkeypatch):
    """llama_cpp module exposing the ``Llama`` class the adapter imports."""
    llama_cpp = ModuleType("llama_cpp")
    llama_cpp.Llama = _FakeLlama
    monkeypatch.setitem(sys.modules, "llama_cpp", llama_cpp)


def _install_hf(monkeypatch):
    """transformers package exposing the classes the adapter imports."""
    fake_tf = ModuleType("transformers")

    class _AutoModel:
        @staticmethod
        def from_pretrained(*a, **k):
            return _FakeHFModel()

    class _AutoTokenizer:
        @staticmethod
        def from_pretrained(*a, **k):
            return _FakeTokenizer()

    fake_tf.AutoModelForCausalLM = _AutoModel
    fake_tf.AutoTokenizer = _AutoTokenizer
    fake_tf.TextIteratorStreamer = object
    monkeypatch.setitem(sys.modules, "transformers", fake_tf)
    monkeypatch.setitem(sys.modules, "torch", _FakeTorch)


def _install_vllm(monkeypatch):
    """vllm package exposing LLM, SamplingParams and the tokenizer submodule."""
    vllm = ModuleType("vllm")
    vllm.LLM = _FakeVLLM.LLM
    vllm.SamplingParams = _FakeVLLM.SamplingParams
    transformers_utils = ModuleType("vllm.transformers_utils")
    transformers_utils.tokenizer = ModuleType("vllm.transformers_utils.tokenizer")
    transformers_utils.tokenizer.get_tokenizer = _FakeVLLM.get_tokenizer
    vllm.transformers_utils = transformers_utils
    monkeypatch.setitem(sys.modules, "vllm", vllm)
    monkeypatch.setitem(sys.modules, "vllm.transformers_utils", transformers_utils)
    monkeypatch.setitem(
        sys.modules, "vllm.transformers_utils.tokenizer", transformers_utils.tokenizer
    )


# --- llama.cpp -------------------------------------------------------------


def test_llamacpp_from_config_requires_path(monkeypatch):
    _install_llama_cpp(monkeypatch)
    from llm.engines import llamacpp

    with pytest.raises(ValueError, match="model_path"):
        llamacpp.LlamaCppEngine.from_config({})


def test_llamacpp_from_config_missing_file(monkeypatch):
    _install_llama_cpp(monkeypatch)
    from llm.engines import llamacpp

    with pytest.raises(FileNotFoundError):
        llamacpp.LlamaCppEngine.from_config({"model_path": "/no/such/file.gguf"})


def test_llamacpp_from_config_hf_repo_ok(monkeypatch):
    _install_llama_cpp(monkeypatch)
    from llm.engines import llamacpp

    e = llamacpp.LlamaCppEngine.from_config(
        {"model": "Qwen/Qwen3-4B-GGUF", "gguf_pattern": "*Q4_K_M*.gguf"}
    )
    assert e.name == "llamacpp"
    assert e._llm is not None


def test_llamacpp_generate_and_stream_chunks(monkeypatch):
    _install_llama_cpp(monkeypatch)
    from llm.engines import llamacpp

    e = llamacpp.LlamaCppEngine.from_config({"model": "Qwen/Qwen3-4B-GGUF"})
    req = LLMRequest(messages=[{"role": "user", "content": "hi"}])
    resp = e.generate(req)
    assert resp.text == "hello world"
    assert resp.finish_reason == "stop"

    chunks = list(e.stream_chunks(req, session_id="s", utterance_id="u"))
    assert len(chunks) == 2
    assert chunks[0].text == "hel" and chunks[0].is_final is False
    assert chunks[-1].text == "lo" and chunks[-1].is_final is True


def test_llamacpp_system_prompt_injected(monkeypatch):
    _install_llama_cpp(monkeypatch)
    from llm.engines import llamacpp

    e = llamacpp.LlamaCppEngine.from_config({"model": "Qwen/Qwen3-4B-GGUF", "system_prompt": "S"})
    req = LLMRequest(messages=[{"role": "user", "content": "hi"}])
    e.generate(req)
    messages = e._llm.calls[0][0]
    assert messages[0] == {"role": "system", "content": "S"}


def test_llamacpp_unload(monkeypatch):
    _install_llama_cpp(monkeypatch)
    _install_fake(monkeypatch, "torch", _FakeTorch)
    from llm.engines import llamacpp

    e = llamacpp.LlamaCppEngine.from_config({"model": "Qwen/Qwen3-4B-GGUF"})
    e.unload()
    assert e._llm is None


# --- SGLang ----------------------------------------------------------------


def test_sglang_from_config_requires_model(monkeypatch):
    _install_fake(monkeypatch, "sglang", _FakeSGL)
    from llm.engines import sglang

    with pytest.raises(ValueError, match="model"):
        sglang.SGLangEngine.from_config({})


def test_sglang_from_config_and_generate(monkeypatch):
    _install_fake(monkeypatch, "sglang", _FakeSGL)
    from llm.engines import sglang

    e = sglang.SGLangEngine.from_config({"model": "Qwen/Qwen3-4B-Instruct"})
    assert e.name == "sglang"
    resp = e.generate(LLMRequest(messages=[{"role": "user", "content": "hi"}]))
    assert resp.text == "sgl hello"
    assert resp.finish_reason == "stop"


def test_sglang_stream_yields_text(monkeypatch):
    _install_fake(monkeypatch, "sglang", _FakeSGL)
    from llm.engines import sglang

    e = sglang.SGLangEngine.from_config({"model": "Qwen/Qwen3-4B-Instruct"})
    assert list(e.stream(LLMRequest(messages=[{"role": "user", "content": "hi"}]))) == ["sgl hello"]


def test_sglang_unload_shuts_down(monkeypatch):
    _install_fake(monkeypatch, "sglang", _FakeSGL)
    from llm.engines import sglang

    e = sglang.SGLangEngine.from_config({"model": "Qwen/Qwen3-4B-Instruct"})
    e.unload()
    assert e._engine is None


# --- vLLM ------------------------------------------------------------------


def test_vllm_from_config_requires_model(monkeypatch):
    _install_vllm(monkeypatch)
    from llm.engines import vllm

    with pytest.raises(ValueError, match="model"):
        vllm.VLLMEngine.from_config({})


def test_vllm_from_config_and_generate(monkeypatch):
    _install_vllm(monkeypatch)
    from llm.engines import vllm

    e = vllm.VLLMEngine.from_config({"model": "Qwen/Qwen3-4B"})
    assert e.name == "vllm"
    resp = e.generate(LLMRequest(messages=[{"role": "user", "content": "hi"}]))
    assert resp.text == "vllm hello"
    assert resp.num_prompt_tokens == 3


def test_vllm_generate_empty_output(monkeypatch):
    _install_vllm(monkeypatch)
    from llm.engines import vllm

    e = vllm.VLLMEngine.from_config({"model": "Qwen/Qwen3-4B"})
    e._llm.generate = lambda *a, **k: []
    resp = e.generate(LLMRequest(messages=[{"role": "user", "content": "hi"}]))
    assert resp.text == ""


def test_vllm_chat_template_fallback_when_no_tokenizer(monkeypatch):
    _install_vllm(monkeypatch)
    from llm.engines import vllm

    e = vllm.VLLMEngine.from_config({"model": "Qwen/Qwen3-4B"})
    e._tokenizer = None
    assert e._apply_chat_template(LLMRequest(messages=[{"role": "user", "content": "hi"}])) == "hi"


def test_vllm_stream_delegates_to_generate(monkeypatch):
    _install_vllm(monkeypatch)
    from llm.engines import vllm

    e = vllm.VLLMEngine.from_config({"model": "Qwen/Qwen3-4B"})
    assert list(e.stream(LLMRequest(messages=[{"role": "user", "content": "hi"}]))) == [
        "vllm hello"
    ]


def test_vllm_unload(monkeypatch):
    _install_vllm(monkeypatch)
    _install_fake(monkeypatch, "torch", _FakeTorch)
    from llm.engines import vllm

    e = vllm.VLLMEngine.from_config({"model": "Qwen/Qwen3-4B"})
    e.unload()
    assert e._llm is None


# --- HF transformers -------------------------------------------------------


def test_hf_from_config_requires_model(monkeypatch):
    _install_hf(monkeypatch)
    from llm.engines import transformers

    with pytest.raises(ValueError, match="model"):
        transformers.HFTransformersEngine.from_config({})


def test_hf_from_config_and_generate(monkeypatch):
    _install_hf(monkeypatch)
    from llm.engines import transformers

    e = transformers.HFTransformersEngine.from_config({"model": "Qwen/Qwen3-4B", "device": "cpu"})
    assert e.name == "transformers"
    assert e._device == "cpu"
    resp = e.generate(LLMRequest(messages=[{"role": "user", "content": "hi"}]))
    assert resp.text == "hf hello"


class _FakeHFModel:
    def eval(self):
        return self

    def cpu(self):
        return self

    def cuda(self):
        return self

    def generate(self, **kwargs):
        import numpy as np

        return np.array([[1, 2, 3, 4, 5]])

    def __call__(self, *a, **k):
        return _FakeHFOut()


class _FakeHFOut:
    def __getitem__(self, k):
        return _FakeHFOut()

    @property
    def shape(self):
        return (1, 3)


def test_hf_unload(monkeypatch):
    from llm.engines import transformers

    e = transformers.HFTransformersEngine()
    e._model = _FakeHFModel()
    e._tokenizer = _FakeTokenizer()
    e.unload()
    assert e._model is None and e._tokenizer is None

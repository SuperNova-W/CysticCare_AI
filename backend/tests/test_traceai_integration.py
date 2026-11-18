import asyncio
import importlib
import json
import os
import sys
import types
import unittest
from contextlib import contextmanager
from unittest.mock import patch

from app import main_openai
from app.integrations import traceai
from app.services import cot_rag_service, openai_rag_init, stepback_agent


class FakeSpan:
    def __init__(self, name):
        self.name = name
        self.attributes = {}
        self.input = None
        self.output = None
        self.exceptions = []

    def set_attribute(self, key, value):
        self.attributes[key] = value

    def set_input(self, value):
        self.input = value

    def set_output(self, value):
        self.output = value

    def record_exception(self, exc):
        self.exceptions.append(exc)


class FakeSpanContext:
    def __init__(self, span):
        self.span = span

    def __enter__(self):
        return self.span

    def __exit__(self, exc_type, exc, tb):
        return False


class FakeTracer:
    def __init__(self, spans):
        self.spans = spans

    def start_as_current_span(self, name, fi_span_kind="chain"):
        span = FakeSpan(name)
        span.attributes["fi_span_kind"] = fi_span_kind
        self.spans.append(span)
        return FakeSpanContext(span)


class BrokenTracer:
    def start_as_current_span(self, name, fi_span_kind="chain"):
        raise RuntimeError("span boom")


class FakeTraceProvider:
    def __init__(self):
        self.flush_count = 0

    def get_tracer(self, name):
        return name

    def force_flush(self, timeout_millis=30000):
        self.flush_count += 1
        return True


class FakeTraceConfig:
    def __init__(self, **kwargs):
        self.kwargs = kwargs


@contextmanager
def fake_context(value):
    yield


def make_fake_trace_modules(spans):
    fi_module = types.ModuleType("fi_instrumentation")

    class FakeFITracer(FakeTracer):
        def __init__(self, tracer, config=None):
            super().__init__(spans)
            self.tracer = tracer
            self.config = config
            fi_module.last_tracer_config = config

    provider = FakeTraceProvider()
    fi_module.FITracer = FakeFITracer
    fi_module.TraceConfig = FakeTraceConfig
    fi_module.register = lambda **kwargs: provider
    fi_module.provider = provider
    fi_module.using_metadata = fake_context
    fi_module.using_session = fake_context
    fi_module.using_user = fake_context

    fi_types_module = types.ModuleType("fi_instrumentation.fi_types")

    class FakeProjectType:
        OBSERVE = "OBSERVE"
        EVALUATE = "EVALUATE"

    fi_types_module.ProjectType = FakeProjectType

    openai_module = types.ModuleType("traceai_openai")

    class FakeOpenAIInstrumentor:
        def instrument(self, tracer_provider=None, config=None):
            fi_module.instrumentor_config = config
            return None

    openai_module.OpenAIInstrumentor = FakeOpenAIInstrumentor

    return {
        "fi_instrumentation": fi_module,
        "fi_instrumentation.fi_types": fi_types_module,
        "traceai_openai": openai_module,
    }


def reload_traceai_module():
    return importlib.reload(traceai)


class TraceAIIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        reload_traceai_module()

    async def asyncTearDown(self):
        reload_traceai_module()

    async def test_enabled_init_succeeds_with_fake_dependencies(self):
        spans = []
        env = {
            "TRACEAI_ENABLED": "true",
            "TRACEAI_PROJECT_NAME": "Trace Test",
            "FI_API_KEY": "key",
            "FI_SECRET_KEY": "secret",
        }

        with patch.dict(sys.modules, make_fake_trace_modules(spans)), patch.dict(os.environ, env):
            module = reload_traceai_module()

            self.assertTrue(module.initialize_traceai())
            status = module.get_traceai_status()

        self.assertTrue(status["enabled"])
        self.assertTrue(status["available"])
        self.assertEqual(status["project"]["name"], "Trace Test")
        self.assertEqual(status["project"]["type_applied"], "OBSERVE")
        self.assertIsNone(status["last_error"])

    async def test_missing_dependencies_reports_disabled_status(self):
        env = {
            "TRACEAI_ENABLED": "true",
            "FI_API_KEY": "key",
            "FI_SECRET_KEY": "secret",
        }
        blocked = {
            "fi_instrumentation": None,
            "fi_instrumentation.fi_types": None,
            "traceai_openai": None,
        }

        with patch.dict(sys.modules, blocked), patch.dict(os.environ, env):
            module = reload_traceai_module()

            self.assertFalse(module.initialize_traceai())
            status = module.get_traceai_status()

        self.assertFalse(status["enabled"])
        self.assertFalse(status["available"])
        self.assertIn("optional Python packages", status["last_error"])

    async def test_trace_context_and_span_fail_open(self):
        module = reload_traceai_module()
        module._TRACE_STATE.update(
            initialized=True,
            enabled=True,
            tracer=BrokenTracer(),
            using_metadata=lambda value: (_ for _ in ()).throw(RuntimeError("context boom")),
            using_session=fake_context,
            using_user=fake_context,
        )

        with module.trace_context(metadata={"route": "/test"}):
            with module.trace_span("broken") as span:
                self.assertIsNone(span)

    async def test_setup_patches_expected_callables(self):
        spans = []
        env = {
            "TRACEAI_ENABLED": "true",
            "FI_API_KEY": "key",
            "FI_SECRET_KEY": "secret",
        }

        with patch.dict(sys.modules, make_fake_trace_modules(spans)), patch.dict(os.environ, env):
            module = reload_traceai_module()
            self.assertTrue(module.setup_traceai(main_module=main_openai))

            self.assertTrue(module._is_wrapped(main_openai.chat_endpoint))
            self.assertTrue(module._is_wrapped(main_openai.generate_chat_stream))
            self.assertTrue(module._is_wrapped(main_openai.generate_bounded_chat_stream))
            self.assertTrue(module._is_wrapped(main_openai.initialize_endpoint))
            self.assertTrue(module._is_wrapped(main_openai.analyze_query_endpoint))
            self.assertTrue(module._is_wrapped(main_openai.stepback_demo_endpoint))
            self.assertTrue(module._is_wrapped(main_openai.followup_endpoint))
            self.assertTrue(module._is_wrapped(cot_rag_service.ChainOfThoughtRAG.retrieve_for_step))
            self.assertTrue(module._is_wrapped(stepback_agent.StepbackAgent.retrieve_with_stepback))

    async def test_cot_and_stepback_use_wrapped_openai_rag_search(self):
        calls = []

        async def fake_search(query, top_k=5):
            calls.append((query, top_k))
            return {"status": "success", "results": []}

        class FakeService:
            def get_chat_completion(self, *args, **kwargs):
                return "What is ADPKD?"

        with patch.object(openai_rag_init, "search_knowledge_base", fake_search):
            cot = cot_rag_service.ChainOfThoughtRAG(FakeService())
            await cot.retrieve_for_step("cot question", top_k=2)

            stepback = stepback_agent.StepbackAgent(FakeService())
            await stepback.retrieve_with_stepback("stepback question", top_k=3)

        self.assertIn(("cot question", 2), calls)
        self.assertIn(("stepback question", 3), calls)

    async def test_chat_stream_emits_request_span_when_consumed(self):
        spans = []
        env = {
            "TRACEAI_ENABLED": "true",
            "FI_API_KEY": "key",
            "FI_SECRET_KEY": "secret",
        }

        async def fake_stream(request):
            yield json.dumps({"type": "chunk", "data": "hello"}) + "\n"
            yield json.dumps({"type": "sources", "data": []}) + "\n"
            yield json.dumps({"type": "done"}) + "\n"

        fake_module = types.SimpleNamespace(generate_chat_stream=fake_stream)
        request = main_openai.ChatRequest(query="What is ADPKD?", session_id="s1")

        with patch.dict(sys.modules, make_fake_trace_modules(spans)), patch.dict(os.environ, env):
            module = reload_traceai_module()
            self.assertTrue(module.initialize_traceai())
            module._patch_stream_generator(fake_module)

            events = [event async for event in fake_module.generate_chat_stream(request)]

        self.assertEqual(len(events), 3)
        self.assertEqual(spans[0].name, "rag.chat_stream_request")
        self.assertEqual(spans[0].attributes["rag.stream_event_count"], 3)
        self.assertEqual(spans[0].attributes["rag.stream_chunk_count"], 1)

    async def _bounded_stream_spans(self, capture: str):
        spans = []
        env = {
            "TRACEAI_ENABLED": "true",
            "TRACEAI_CAPTURE_CONTENT": capture,
            "FI_HIDE_INPUTS": "true",
            "FI_HIDE_OUTPUTS": "true",
            "FI_API_KEY": "key",
            "FI_SECRET_KEY": "secret",
        }
        received = {}

        async def fake_bounded_stream(request, http_request):
            received["http_request"] = http_request
            yield json.dumps({"type": "chunk", "data": "ADPKD is "}) + "\n"
            yield json.dumps({"type": "chunk", "data": "a kidney disease."}) + "\n"
            yield json.dumps({"type": "done"}) + "\n"

        fake_module = types.SimpleNamespace(generate_bounded_chat_stream=fake_bounded_stream)
        request = main_openai.ChatRequest(query="What is ADPKD?", session_id="patient-1")
        fakes = make_fake_trace_modules(spans)

        with patch.dict(sys.modules, fakes), patch.dict(os.environ, env):
            module = reload_traceai_module()
            self.assertTrue(module.initialize_traceai())
            module._patch_bounded_stream_generator(fake_module)
            events = [e async for e in fake_module.generate_bounded_chat_stream(request, "HTTP_REQ")]

        self.assertEqual(len(events), 3)
        self.assertEqual(received["http_request"], "HTTP_REQ")
        return spans, fakes["fi_instrumentation"]

    async def test_bounded_stream_records_question_and_answer_when_capture_enabled(self):
        spans, fi = await self._bounded_stream_spans("true")

        self.assertEqual(spans[0].name, "rag.chat_stream_request")
        self.assertEqual(spans[0].input, "What is ADPKD?")
        self.assertEqual(spans[0].output, "ADPKD is a kidney disease.")
        self.assertEqual(spans[0].attributes["rag.stream_chunk_count"], 2)
        # Regression: FI_HIDE_* must not redact the opted-in question/answer...
        self.assertEqual(fi.last_tracer_config.kwargs, {"hide_inputs": False, "hide_outputs": False})
        # ...while provider-level OpenAI spans always stay redacted.
        self.assertEqual(fi.instrumentor_config.kwargs, {"hide_inputs": True, "hide_outputs": True})
        # Spans are flushed before the response ends (Cloud Run CPU throttling).
        self.assertGreaterEqual(fi.provider.flush_count, 1)

    async def test_bounded_stream_records_no_text_when_capture_disabled(self):
        spans, fi = await self._bounded_stream_spans("false")

        self.assertIsNone(spans[0].input)
        self.assertIsNone(spans[0].output)
        self.assertEqual(fi.last_tracer_config.kwargs, {"hide_inputs": True, "hide_outputs": True})

    async def test_chat_endpoint_wrapper_passes_http_request_through(self):
        spans = []
        env = {
            "TRACEAI_ENABLED": "true",
            "TRACEAI_CAPTURE_CONTENT": "true",
            "FI_API_KEY": "key",
            "FI_SECRET_KEY": "secret",
        }
        received = {}

        async def fake_chat_endpoint(request, http_request=None):
            received["http_request"] = http_request
            return types.SimpleNamespace(response="An answer", status="success", sources=[], followup_questions=[])

        fake_module = types.SimpleNamespace(
            chat_endpoint=fake_chat_endpoint,
            is_refusal_response=lambda text: False,
        )
        request = main_openai.ChatRequest(query="What is ADPKD?", session_id="patient-1")

        with patch.dict(sys.modules, make_fake_trace_modules(spans)), patch.dict(os.environ, env):
            module = reload_traceai_module()
            self.assertTrue(module.initialize_traceai())
            module._patch_chat_endpoint(fake_module)
            response = await fake_module.chat_endpoint(request=request, http_request="HTTP_REQ")

        self.assertEqual(response.response, "An answer")
        self.assertEqual(received["http_request"], "HTTP_REQ")
        self.assertEqual(spans[0].input, "What is ADPKD?")
        self.assertEqual(spans[0].output, "An answer")

    async def test_health_includes_traceai_status(self):
        expected_status = {"enabled": True, "last_error": None}

        with (
            patch.object(main_openai, "_rag_initialized", False),
            patch.object(main_openai, "get_traceai_status", lambda: expected_status),
        ):
            response = await main_openai.health_endpoint()

        self.assertEqual(response.traceai, expected_status)

    async def test_exception_handlers_return_json_responses(self):
        http_response = await main_openai.http_exception_handler(
            None,
            main_openai.HTTPException(status_code=418, detail="teapot"),
        )
        general_response = await main_openai.general_exception_handler(
            None,
            RuntimeError("boom"),
        )

        self.assertEqual(http_response.status_code, 418)
        self.assertEqual(json.loads(http_response.body), {
            "status": "error",
            "detail": "teapot",
            "status_code": 418,
        })
        self.assertEqual(general_response.status_code, 500)
        self.assertEqual(json.loads(general_response.body), {
            "status": "error",
            "detail": "Internal server error",
            "message": "boom",
        })

    async def test_validation_wrapper_preserves_positional_compatibility(self):
        spans = []
        env = {
            "TRACEAI_ENABLED": "true",
            "FI_API_KEY": "key",
            "FI_SECRET_KEY": "secret",
        }

        def fake_regenerate(self, *args, **kwargs):
            return "improved"

        validation_agent = importlib.import_module("app.services.validation_agent")
        validation_agent = importlib.reload(validation_agent)

        async def fake_validate(self, query, answer, retrieved_chunks, agent_type="standard_rag"):
            return validation_agent.ValidationResult(
                passed=True,
                checks=[],
                overall_score=1.0,
                feedback="ok",
            )

        with (
            patch.dict(sys.modules, make_fake_trace_modules(spans)),
            patch.dict(os.environ, env),
            patch.object(validation_agent.ValidationAgent, "validate", fake_validate),
            patch.object(validation_agent.ValidationAgent, "_regenerate_with_feedback", fake_regenerate),
        ):
            module = reload_traceai_module()

            module._patch_validation_agent(validation_agent)
            agent = validation_agent.ValidationAgent(object())
            result = await agent.validate_and_retry("q", "answer", [], "standard_rag", 0.2, 123)

        self.assertEqual(result["answer"], "answer")
        self.assertFalse(result["was_regenerated"])


if __name__ == "__main__":
    unittest.main()

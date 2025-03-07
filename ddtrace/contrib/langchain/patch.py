import os
import sys
from typing import Any
from typing import Dict
from typing import Optional
from typing import Union

import langchain
import langchain_community
import langchain_core


try:
    import langchain_openai
except ImportError:
    langchain_openai = None
try:
    import langchain_pinecone
except ImportError:
    langchain_pinecone = None

from ddtrace.appsec._iast import _is_iast_enabled


try:
    from langchain.callbacks.openai_info import get_openai_token_cost_for_model
except ImportError:
    from langchain_community.callbacks.openai_info import get_openai_token_cost_for_model
from pydantic import SecretStr

from ddtrace import Span
from ddtrace import config
from ddtrace.contrib.langchain.constants import API_KEY
from ddtrace.contrib.langchain.constants import COMPLETION_TOKENS
from ddtrace.contrib.langchain.constants import MODEL
from ddtrace.contrib.langchain.constants import PROMPT_TOKENS
from ddtrace.contrib.langchain.constants import TOTAL_COST
from ddtrace.contrib.langchain.constants import agent_output_parser_classes
from ddtrace.contrib.langchain.constants import text_embedding_models
from ddtrace.contrib.langchain.constants import vectorstore_classes
from ddtrace.contrib.trace_utils import unwrap
from ddtrace.contrib.trace_utils import with_traced_module
from ddtrace.contrib.trace_utils import wrap
from ddtrace.internal.logger import get_logger
from ddtrace.internal.utils import ArgumentError
from ddtrace.internal.utils import get_argument_value
from ddtrace.internal.utils.formats import asbool
from ddtrace.internal.utils.formats import deep_getattr
from ddtrace.internal.utils.version import parse_version
from ddtrace.llmobs import LLMObs
from ddtrace.llmobs._integrations import LangChainIntegration
from ddtrace.pin import Pin
from ddtrace.vendor import wrapt


log = get_logger(__name__)


def get_version():
    # type: () -> str
    return getattr(langchain, "__version__", "")


# After 0.1.0, implementation split into langchain, langchain_community, and langchain_core.
# We need to check the version to determine which module to wrap, to avoid deprecation warnings
# ref: https://github.com/DataDog/dd-trace-py/issues/8212
LANGCHAIN_VERSION = parse_version(get_version())
SHOULD_PATCH_LANGCHAIN_COMMUNITY = LANGCHAIN_VERSION >= (0, 1, 0)
BASE_LANGCHAIN_MODULE = langchain_community if SHOULD_PATCH_LANGCHAIN_COMMUNITY else langchain
BASE_LANGCHAIN_MODULE_NAME = getattr(BASE_LANGCHAIN_MODULE, "__name__", "langchain")


config._add(
    "langchain",
    {
        "logs_enabled": asbool(os.getenv("DD_LANGCHAIN_LOGS_ENABLED", False)),
        "metrics_enabled": asbool(os.getenv("DD_LANGCHAIN_METRICS_ENABLED", True)),
        "span_prompt_completion_sample_rate": float(os.getenv("DD_LANGCHAIN_SPAN_PROMPT_COMPLETION_SAMPLE_RATE", 1.0)),
        "log_prompt_completion_sample_rate": float(os.getenv("DD_LANGCHAIN_LOG_PROMPT_COMPLETION_SAMPLE_RATE", 0.1)),
        "span_char_limit": int(os.getenv("DD_LANGCHAIN_SPAN_CHAR_LIMIT", 128)),
    },
)


def _extract_model_name(instance: Any) -> Optional[str]:
    """Extract model name or ID from llm instance."""
    for attr in ("model", "model_name", "model_id", "model_key", "repo_id"):
        if hasattr(instance, attr):
            return getattr(instance, attr)
    return None


def _format_api_key(api_key: Union[str, SecretStr]) -> str:
    """Obfuscate a given LLM provider API key by returning the last four characters."""
    if hasattr(api_key, "get_secret_value"):
        api_key = api_key.get_secret_value()

    if not api_key or len(api_key) < 4:
        return ""
    return "...%s" % api_key[-4:]


def _extract_api_key(instance: Any) -> str:
    """
    Extract and format LLM-provider API key from instance.
    Note that langchain's LLM/ChatModel/Embeddings interfaces do not have a
    standard attribute name for storing the provider-specific API key, so make a
    best effort here by checking for attributes that end with `api_key/api_token`.
    """
    api_key_attrs = [a for a in dir(instance) if a.endswith(("api_token", "api_key"))]
    if api_key_attrs and hasattr(instance, str(api_key_attrs[0])):
        api_key = getattr(instance, api_key_attrs[0], None)
        if api_key:
            return _format_api_key(api_key)
    return ""


def _tag_openai_token_usage(
    span: Span, llm_output: Dict[str, Any], propagated_cost: int = 0, propagate: bool = False
) -> None:
    """
    Extract token usage from llm_output, tag on span.
    Calculate the total cost for each LLM/chat_model, then propagate those values up the trace so that
    the root span will store the total token_usage/cost of all of its descendants.
    """
    for token_type in ("prompt", "completion", "total"):
        current_metric_value = span.get_metric("langchain.tokens.%s_tokens" % token_type) or 0
        metric_value = llm_output["token_usage"].get("%s_tokens" % token_type, 0)
        span.set_metric("langchain.tokens.%s_tokens" % token_type, current_metric_value + metric_value)
    total_cost = span.get_metric(TOTAL_COST) or 0
    if not propagate:
        try:
            completion_cost = get_openai_token_cost_for_model(
                span.get_tag(MODEL),
                span.get_metric(COMPLETION_TOKENS),
                is_completion=True,
            )
            prompt_cost = get_openai_token_cost_for_model(span.get_tag(MODEL), span.get_metric(PROMPT_TOKENS))
            total_cost = completion_cost + prompt_cost
        except ValueError:
            # If not in langchain's openai model catalog, the above helpers will raise a ValueError.
            log.debug("Cannot calculate token/cost as the model is not in LangChain's OpenAI model catalog.")
    span.set_metric(TOTAL_COST, propagated_cost + total_cost)
    if span._parent is not None:
        _tag_openai_token_usage(span._parent, llm_output, propagated_cost=propagated_cost + total_cost, propagate=True)


@with_traced_module
def traced_llm_generate(langchain, pin, func, instance, args, kwargs):
    llm_provider = instance._llm_type
    prompts = get_argument_value(args, kwargs, 0, "prompts")
    integration = langchain._datadog_integration
    model = _extract_model_name(instance)
    span = integration.trace(
        pin,
        "%s.%s" % (instance.__module__, instance.__class__.__name__),
        submit_to_llmobs=True,
        interface_type="llm",
        provider=llm_provider,
        model=model,
        api_key=_extract_api_key(instance),
    )
    completions = None
    try:
        if integration.is_pc_sampled_span(span):
            for idx, prompt in enumerate(prompts):
                span.set_tag_str("langchain.request.prompts.%d" % idx, integration.trunc(str(prompt)))
        for param, val in getattr(instance, "_identifying_params", {}).items():
            if isinstance(val, dict):
                for k, v in val.items():
                    span.set_tag_str("langchain.request.%s.parameters.%s.%s" % (llm_provider, param, k), str(v))
            else:
                span.set_tag_str("langchain.request.%s.parameters.%s" % (llm_provider, param), str(val))

        completions = func(*args, **kwargs)
        if isinstance(instance, BASE_LANGCHAIN_MODULE.llms.OpenAI) or (
            langchain_openai and isinstance(instance, langchain_openai.OpenAI)
        ):
            _tag_openai_token_usage(span, completions.llm_output)
            integration.record_usage(span, completions.llm_output)

        for idx, completion in enumerate(completions.generations):
            if integration.is_pc_sampled_span(span):
                span.set_tag_str("langchain.response.completions.%d.text" % idx, integration.trunc(completion[0].text))
            if completion and completion[0].generation_info is not None:
                span.set_tag_str(
                    "langchain.response.completions.%d.finish_reason" % idx,
                    str(completion[0].generation_info.get("finish_reason")),
                )
                span.set_tag_str(
                    "langchain.response.completions.%d.logprobs" % idx,
                    str(completion[0].generation_info.get("logprobs")),
                )
    except Exception:
        span.set_exc_info(*sys.exc_info())
        integration.metric(span, "incr", "request.error", 1)
        raise
    finally:
        if integration.is_pc_sampled_llmobs(span):
            integration.llmobs_set_tags(
                "llm",
                span,
                prompts,
                completions,
                error=bool(span.error),
            )
        span.finish()
        integration.metric(span, "dist", "request.duration", span.duration_ns)
        if integration.is_pc_sampled_log(span):
            if completions is None:
                log_completions = []
            else:
                log_completions = [
                    [{"text": completion.text} for completion in completions] for completions in completions.generations
                ]
            integration.log(
                span,
                "info" if span.error == 0 else "error",
                "sampled %s.%s" % (instance.__module__, instance.__class__.__name__),
                attrs={
                    "prompts": prompts,
                    "choices": log_completions,
                },
            )
    return completions


@with_traced_module
async def traced_llm_agenerate(langchain, pin, func, instance, args, kwargs):
    llm_provider = instance._llm_type
    prompts = get_argument_value(args, kwargs, 0, "prompts")
    integration = langchain._datadog_integration
    model = _extract_model_name(instance)
    span = integration.trace(
        pin,
        "%s.%s" % (instance.__module__, instance.__class__.__name__),
        submit_to_llmobs=True,
        interface_type="llm",
        provider=llm_provider,
        model=model,
        api_key=_extract_api_key(instance),
    )
    completions = None
    try:
        if integration.is_pc_sampled_span(span):
            for idx, prompt in enumerate(prompts):
                span.set_tag_str("langchain.request.prompts.%d" % idx, integration.trunc(str(prompt)))
        for param, val in getattr(instance, "_identifying_params", {}).items():
            if isinstance(val, dict):
                for k, v in val.items():
                    span.set_tag_str("langchain.request.%s.parameters.%s.%s" % (llm_provider, param, k), str(v))
            else:
                span.set_tag_str("langchain.request.%s.parameters.%s" % (llm_provider, param), str(val))

        completions = await func(*args, **kwargs)
        if isinstance(instance, BASE_LANGCHAIN_MODULE.llms.OpenAI) or (
            langchain_openai and isinstance(instance, langchain_openai.OpenAI)
        ):
            _tag_openai_token_usage(span, completions.llm_output)
            integration.record_usage(span, completions.llm_output)

        for idx, completion in enumerate(completions.generations):
            if integration.is_pc_sampled_span(span):
                span.set_tag_str("langchain.response.completions.%d.text" % idx, integration.trunc(completion[0].text))
            if completion and completion[0].generation_info is not None:
                span.set_tag_str(
                    "langchain.response.completions.%d.finish_reason" % idx,
                    str(completion[0].generation_info.get("finish_reason")),
                )
                span.set_tag_str(
                    "langchain.response.completions.%d.logprobs" % idx,
                    str(completion[0].generation_info.get("logprobs")),
                )
    except Exception:
        span.set_exc_info(*sys.exc_info())
        integration.metric(span, "incr", "request.error", 1)
        raise
    finally:
        if integration.is_pc_sampled_llmobs(span):
            integration.llmobs_set_tags(
                "llm",
                span,
                prompts,
                completions,
                error=bool(span.error),
            )
        span.finish()
        integration.metric(span, "dist", "request.duration", span.duration_ns)
        if integration.is_pc_sampled_log(span):
            if completions is None:
                log_completions = []
            else:
                log_completions = [
                    [{"text": completion.text} for completion in completions] for completions in completions.generations
                ]
            integration.log(
                span,
                "info" if span.error == 0 else "error",
                "sampled %s.%s" % (instance.__module__, instance.__class__.__name__),
                attrs={
                    "prompts": prompts,
                    "choices": log_completions,
                },
            )
    return completions


@with_traced_module
def traced_chat_model_generate(langchain, pin, func, instance, args, kwargs):
    llm_provider = instance._llm_type.split("-")[0]
    chat_messages = get_argument_value(args, kwargs, 0, "messages")
    integration = langchain._datadog_integration
    span = integration.trace(
        pin,
        "%s.%s" % (instance.__module__, instance.__class__.__name__),
        submit_to_llmobs=True,
        interface_type="chat_model",
        provider=llm_provider,
        model=_extract_model_name(instance),
        api_key=_extract_api_key(instance),
    )
    chat_completions = None
    try:
        for message_set_idx, message_set in enumerate(chat_messages):
            for message_idx, message in enumerate(message_set):
                if integration.is_pc_sampled_span(span):
                    if isinstance(message, dict):
                        span.set_tag_str(
                            "langchain.request.messages.%d.%d.content" % (message_set_idx, message_idx),
                            integration.trunc(str(message.get("content", ""))),
                        )
                    else:
                        span.set_tag_str(
                            "langchain.request.messages.%d.%d.content" % (message_set_idx, message_idx),
                            integration.trunc(str(getattr(message, "content", ""))),
                        )
                span.set_tag_str(
                    "langchain.request.messages.%d.%d.message_type" % (message_set_idx, message_idx),
                    message.__class__.__name__,
                )
        for param, val in getattr(instance, "_identifying_params", {}).items():
            if isinstance(val, dict):
                for k, v in val.items():
                    span.set_tag_str("langchain.request.%s.parameters.%s.%s" % (llm_provider, param, k), str(v))
            else:
                span.set_tag_str("langchain.request.%s.parameters.%s" % (llm_provider, param), str(val))

        chat_completions = func(*args, **kwargs)
        if isinstance(instance, BASE_LANGCHAIN_MODULE.chat_models.ChatOpenAI) or (
            langchain_openai and isinstance(instance, langchain_openai.ChatOpenAI)
        ):
            _tag_openai_token_usage(span, chat_completions.llm_output)
            integration.record_usage(span, chat_completions.llm_output)

        for message_set_idx, message_set in enumerate(chat_completions.generations):
            for idx, chat_completion in enumerate(message_set):
                if integration.is_pc_sampled_span(span):
                    span.set_tag_str(
                        "langchain.response.completions.%d.%d.content" % (message_set_idx, idx),
                        integration.trunc(chat_completion.text),
                    )
                span.set_tag_str(
                    "langchain.response.completions.%d.%d.message_type" % (message_set_idx, idx),
                    chat_completion.message.__class__.__name__,
                )
    except Exception:
        span.set_exc_info(*sys.exc_info())
        integration.metric(span, "incr", "request.error", 1)
        raise
    finally:
        if integration.is_pc_sampled_llmobs(span):
            integration.llmobs_set_tags(
                "chat",
                span,
                chat_messages,
                chat_completions,
                error=bool(span.error),
            )
        span.finish()
        integration.metric(span, "dist", "request.duration", span.duration_ns)
        if integration.is_pc_sampled_log(span):
            if chat_completions is None:
                log_chat_completions = []
            else:
                log_chat_completions = [
                    [
                        {"content": message.text, "message_type": message.message.__class__.__name__}
                        for message in messages
                    ]
                    for messages in chat_completions.generations
                ]
            integration.log(
                span,
                "info" if span.error == 0 else "error",
                "sampled %s.%s" % (instance.__module__, instance.__class__.__name__),
                attrs={
                    "messages": [
                        [
                            {
                                "content": message.get("content", "")
                                if isinstance(message, dict)
                                else str(getattr(message, "content", "")),
                                "message_type": message.__class__.__name__,
                            }
                            for message in messages
                        ]
                        for messages in chat_messages
                    ],
                    "choices": log_chat_completions,
                },
            )
    return chat_completions


@with_traced_module
async def traced_chat_model_agenerate(langchain, pin, func, instance, args, kwargs):
    llm_provider = instance._llm_type.split("-")[0]
    chat_messages = get_argument_value(args, kwargs, 0, "messages")
    integration = langchain._datadog_integration
    span = integration.trace(
        pin,
        "%s.%s" % (instance.__module__, instance.__class__.__name__),
        submit_to_llmobs=True,
        interface_type="chat_model",
        provider=llm_provider,
        model=_extract_model_name(instance),
        api_key=_extract_api_key(instance),
    )
    chat_completions = None
    try:
        for message_set_idx, message_set in enumerate(chat_messages):
            for message_idx, message in enumerate(message_set):
                if integration.is_pc_sampled_span(span):
                    if isinstance(message, dict):
                        span.set_tag_str(
                            "langchain.request.messages.%d.%d.content" % (message_set_idx, message_idx),
                            integration.trunc(str(message.get("content", ""))),
                        )
                    else:
                        span.set_tag_str(
                            "langchain.request.messages.%d.%d.content" % (message_set_idx, message_idx),
                            integration.trunc(str(getattr(message, "content", ""))),
                        )
                span.set_tag_str(
                    "langchain.request.messages.%d.%d.message_type" % (message_set_idx, message_idx),
                    message.__class__.__name__,
                )
        for param, val in getattr(instance, "_identifying_params", {}).items():
            if isinstance(val, dict):
                for k, v in val.items():
                    span.set_tag_str("langchain.request.%s.parameters.%s.%s" % (llm_provider, param, k), str(v))
            else:
                span.set_tag_str("langchain.request.%s.parameters.%s" % (llm_provider, param), str(val))

        chat_completions = await func(*args, **kwargs)
        if isinstance(instance, BASE_LANGCHAIN_MODULE.chat_models.ChatOpenAI) or (
            langchain_openai and isinstance(instance, langchain_openai.ChatOpenAI)
        ):
            _tag_openai_token_usage(span, chat_completions.llm_output)
            integration.record_usage(span, chat_completions.llm_output)

        for message_set_idx, message_set in enumerate(chat_completions.generations):
            for idx, chat_completion in enumerate(message_set):
                if integration.is_pc_sampled_span(span):
                    span.set_tag_str(
                        "langchain.response.completions.%d.%d.content" % (message_set_idx, idx),
                        integration.trunc(chat_completion.text),
                    )
                span.set_tag_str(
                    "langchain.response.completions.%d.%d.message_type" % (message_set_idx, idx),
                    chat_completion.message.__class__.__name__,
                )
    except Exception:
        span.set_exc_info(*sys.exc_info())
        integration.metric(span, "incr", "request.error", 1)
        raise
    finally:
        if integration.is_pc_sampled_llmobs(span):
            integration.llmobs_set_tags(
                "chat",
                span,
                chat_messages,
                chat_completions,
                error=bool(span.error),
            )
        span.finish()
        integration.metric(span, "dist", "request.duration", span.duration_ns)
        if integration.is_pc_sampled_log(span):
            if chat_completions is None:
                log_chat_completions = []
            else:
                log_chat_completions = [
                    [
                        {"content": message.text, "message_type": message.message.__class__.__name__}
                        for message in messages
                    ]
                    for messages in chat_completions.generations
                ]
            integration.log(
                span,
                "info" if span.error == 0 else "error",
                "sampled %s.%s" % (instance.__module__, instance.__class__.__name__),
                attrs={
                    "messages": [
                        [
                            {
                                "content": message.get("content", "")
                                if isinstance(message, dict)
                                else str(getattr(message, "content", "")),
                                "message_type": message.__class__.__name__,
                            }
                            for message in messages
                        ]
                        for messages in chat_messages
                    ],
                    "choices": log_chat_completions,
                },
            )
    return chat_completions


@with_traced_module
def traced_embedding(langchain, pin, func, instance, args, kwargs):
    """
    This traces both embed_query(text) and embed_documents(texts), so we need to make sure
    we get the right arg/kwarg.
    """
    try:
        input_texts = get_argument_value(args, kwargs, 0, "texts")
    except ArgumentError:
        input_texts = get_argument_value(args, kwargs, 0, "text")

    provider = instance.__class__.__name__.split("Embeddings")[0].lower()
    integration = langchain._datadog_integration
    span = integration.trace(
        pin,
        "%s.%s" % (instance.__module__, instance.__class__.__name__),
        interface_type="embedding",
        provider=provider,
        model=_extract_model_name(instance),
        api_key=_extract_api_key(instance),
    )
    try:
        if isinstance(input_texts, str):
            if integration.is_pc_sampled_span(span):
                span.set_tag_str("langchain.request.inputs.0.text", integration.trunc(input_texts))
            span.set_metric("langchain.request.input_count", 1)
        else:
            if integration.is_pc_sampled_span(span):
                for idx, text in enumerate(input_texts):
                    span.set_tag_str("langchain.request.inputs.%d.text" % idx, integration.trunc(text))
            span.set_metric("langchain.request.input_count", len(input_texts))
        # langchain currently does not support token tracking for OpenAI embeddings:
        #  https://github.com/hwchase17/langchain/issues/945
        embeddings = func(*args, **kwargs)
        if isinstance(embeddings, list) and embeddings and isinstance(embeddings[0], list):
            for idx, embedding in enumerate(embeddings):
                span.set_metric("langchain.response.outputs.%d.embedding_length" % idx, len(embedding))
        else:
            span.set_metric("langchain.response.outputs.embedding_length", len(embeddings))
    except Exception:
        span.set_exc_info(*sys.exc_info())
        integration.metric(span, "incr", "request.error", 1)
        raise
    finally:
        span.finish()
        integration.metric(span, "dist", "request.duration", span.duration_ns)
        if integration.is_pc_sampled_log(span):
            integration.log(
                span,
                "info" if span.error == 0 else "error",
                "sampled %s.%s" % (instance.__module__, instance.__class__.__name__),
                attrs={"inputs": [input_texts] if isinstance(input_texts, str) else input_texts},
            )
    return embeddings


@with_traced_module
def traced_chain_call(langchain, pin, func, instance, args, kwargs):
    integration = langchain._datadog_integration
    span = integration.trace(pin, "%s.%s" % (instance.__module__, instance.__class__.__name__), interface_type="chain")
    final_outputs = {}
    try:
        if SHOULD_PATCH_LANGCHAIN_COMMUNITY:
            inputs = get_argument_value(args, kwargs, 0, "input")
        else:
            inputs = get_argument_value(args, kwargs, 0, "inputs")
        if not isinstance(inputs, dict):
            inputs = {instance.input_keys[0]: inputs}
        if integration.is_pc_sampled_span(span):
            for k, v in inputs.items():
                span.set_tag_str("langchain.request.inputs.%s" % k, integration.trunc(str(v)))
            template = deep_getattr(instance, "prompt.template", default="")
            if template:
                span.set_tag_str("langchain.request.prompt", integration.trunc(str(template)))
        final_outputs = func(*args, **kwargs)
        if integration.is_pc_sampled_span(span):
            for k, v in final_outputs.items():
                span.set_tag_str("langchain.response.outputs.%s" % k, integration.trunc(str(v)))
        if _is_iast_enabled():
            taint_outputs(instance, inputs, final_outputs)
    except Exception:
        span.set_exc_info(*sys.exc_info())
        integration.metric(span, "incr", "request.error", 1)
        raise
    finally:
        span.finish()
        integration.metric(span, "dist", "request.duration", span.duration_ns)
        if integration.is_pc_sampled_log(span):
            log_inputs = {}
            log_outputs = {}
            for k, v in inputs.items():
                log_inputs[k] = str(v)
            for k, v in final_outputs.items():
                log_outputs[k] = str(v)
            integration.log(
                span,
                "info" if span.error == 0 else "error",
                "sampled %s.%s" % (instance.__module__, instance.__class__.__name__),
                attrs={
                    "inputs": log_inputs,
                    "prompt": str(deep_getattr(instance, "prompt.template", default="")),
                    "outputs": log_outputs,
                },
            )
    return final_outputs


@with_traced_module
async def traced_chain_acall(langchain, pin, func, instance, args, kwargs):
    integration = langchain._datadog_integration
    span = integration.trace(pin, "%s.%s" % (instance.__module__, instance.__class__.__name__), interface_type="chain")
    final_outputs = {}
    try:
        if SHOULD_PATCH_LANGCHAIN_COMMUNITY:
            inputs = get_argument_value(args, kwargs, 0, "input")
        else:
            inputs = get_argument_value(args, kwargs, 0, "inputs")
        if not isinstance(inputs, dict):
            inputs = {instance.input_keys[0]: inputs}
        if integration.is_pc_sampled_span(span):
            for k, v in inputs.items():
                span.set_tag_str("langchain.request.inputs.%s" % k, integration.trunc(str(v)))
            template = deep_getattr(instance, "prompt.template", default="")
            if template:
                span.set_tag_str("langchain.request.prompt", integration.trunc(str(template)))
        final_outputs = await func(*args, **kwargs)
        if integration.is_pc_sampled_span(span):
            for k, v in final_outputs.items():
                span.set_tag_str("langchain.response.outputs.%s" % k, integration.trunc(str(v)))
    except Exception:
        span.set_exc_info(*sys.exc_info())
        integration.metric(span, "incr", "request.error", 1)
        raise
    finally:
        span.finish()
        integration.metric(span, "dist", "request.duration", span.duration_ns)
        if integration.is_pc_sampled_log(span):
            log_inputs = {}
            log_outputs = {}
            for k, v in inputs.items():
                log_inputs[k] = str(v)
            for k, v in final_outputs.items():
                log_outputs[k] = str(v)
            integration.log(
                span,
                "info" if span.error == 0 else "error",
                "sampled %s.%s" % (instance.__module__, instance.__class__.__name__),
                attrs={
                    "inputs": log_inputs,
                    "prompt": str(deep_getattr(instance, "prompt.template", default="")),
                    "outputs": log_outputs,
                },
            )
    return final_outputs


@with_traced_module
def traced_similarity_search(langchain, pin, func, instance, args, kwargs):
    integration = langchain._datadog_integration
    query = get_argument_value(args, kwargs, 0, "query")
    k = kwargs.get("k", args[1] if len(args) >= 2 else None)
    provider = instance.__class__.__name__.lower()
    span = integration.trace(
        pin,
        "%s.%s" % (instance.__module__, instance.__class__.__name__),
        interface_type="similarity_search",
        provider=provider,
        api_key=_extract_api_key(instance),
    )
    documents = []
    try:
        if integration.is_pc_sampled_span(span):
            span.set_tag_str("langchain.request.query", integration.trunc(query))
        if k is not None:
            span.set_tag_str("langchain.request.k", str(k))
        for kwarg_key, v in kwargs.items():
            span.set_tag_str("langchain.request.%s" % kwarg_key, str(v))
        if isinstance(instance, BASE_LANGCHAIN_MODULE.vectorstores.Pinecone) and hasattr(
            instance._index, "configuration"
        ):
            span.set_tag_str(
                "langchain.request.pinecone.environment",
                instance._index.configuration.server_variables.get("environment", ""),
            )
            span.set_tag_str(
                "langchain.request.pinecone.index_name",
                instance._index.configuration.server_variables.get("index_name", ""),
            )
            span.set_tag_str(
                "langchain.request.pinecone.project_name",
                instance._index.configuration.server_variables.get("project_name", ""),
            )
            api_key = instance._index.configuration.api_key.get("ApiKeyAuth", "")
            span.set_tag_str(API_KEY, _format_api_key(api_key))  # override api_key for Pinecone
        documents = func(*args, **kwargs)
        span.set_metric("langchain.response.document_count", len(documents))
        for idx, document in enumerate(documents):
            span.set_tag_str(
                "langchain.response.document.%d.page_content" % idx, integration.trunc(str(document.page_content))
            )
            for kwarg_key, v in document.metadata.items():
                span.set_tag_str(
                    "langchain.response.document.%d.metadata.%s" % (idx, kwarg_key), integration.trunc(str(v))
                )
    except Exception:
        span.set_exc_info(*sys.exc_info())
        integration.metric(span, "incr", "request.error", 1)
        raise
    finally:
        span.finish()
        integration.metric(span, "dist", "request.duration", span.duration_ns)
        if integration.is_pc_sampled_log(span):
            integration.log(
                span,
                "info" if span.error == 0 else "error",
                "sampled %s.%s" % (instance.__module__, instance.__class__.__name__),
                attrs={
                    "query": query,
                    "k": k or "",
                    "documents": [
                        {"page_content": document.page_content, "metadata": document.metadata} for document in documents
                    ],
                },
            )
    return documents


def patch():
    if getattr(langchain, "_datadog_patch", False):
        return

    if config._llmobs_enabled:
        LLMObs.enable()

    langchain._datadog_patch = True

    Pin().onto(langchain)
    integration = LangChainIntegration(integration_config=config.langchain)
    langchain._datadog_integration = integration

    # Langchain doesn't allow wrapping directly from root, so we have to import the base classes first before wrapping.
    # ref: https://github.com/DataDog/dd-trace-py/issues/7123
    if SHOULD_PATCH_LANGCHAIN_COMMUNITY:
        from langchain.chains.base import Chain  # noqa:F401
        from langchain_community import chat_models  # noqa:F401
        from langchain_community import embeddings  # noqa:F401
        from langchain_community import llms  # noqa:F401
        from langchain_community import vectorstores  # noqa:F401

        wrap("langchain_core", "language_models.llms.BaseLLM.generate", traced_llm_generate(langchain))
        wrap("langchain_core", "language_models.llms.BaseLLM.agenerate", traced_llm_agenerate(langchain))
        wrap(
            "langchain_core",
            "language_models.chat_models.BaseChatModel.generate",
            traced_chat_model_generate(langchain),
        )
        wrap(
            "langchain_core",
            "language_models.chat_models.BaseChatModel.agenerate",
            traced_chat_model_agenerate(langchain),
        )
        wrap("langchain", "chains.base.Chain.invoke", traced_chain_call(langchain))
        wrap("langchain", "chains.base.Chain.ainvoke", traced_chain_acall(langchain))
        if langchain_openai:
            wrap("langchain_openai", "OpenAIEmbeddings.embed_documents", traced_embedding(langchain))
        if langchain_pinecone:
            wrap("langchain_pinecone", "PineconeVectorStore.similarity_search", traced_similarity_search(langchain))
    else:
        from langchain import embeddings  # noqa:F401
        from langchain import vectorstores  # noqa:F401
        from langchain.chains.base import Chain  # noqa:F401
        from langchain.chat_models.base import BaseChatModel  # noqa:F401
        from langchain.llms.base import BaseLLM  # noqa:F401

        wrap("langchain", "llms.base.BaseLLM.generate", traced_llm_generate(langchain))
        wrap("langchain", "llms.base.BaseLLM.agenerate", traced_llm_agenerate(langchain))
        wrap(
            "langchain", "chat_models.base.BaseChatModel.generate", traced_chat_model_generate(langchain)
        )  # might need to change back to langchain_community
        wrap(
            "langchain", "chat_models.base.BaseChatModel.agenerate", traced_chat_model_agenerate(langchain)
        )  # might need to change back to langchain_community
        wrap("langchain", "chains.base.Chain.__call__", traced_chain_call(langchain))
        wrap("langchain", "chains.base.Chain.acall", traced_chain_acall(langchain))
        wrap("langchain", "embeddings.OpenAIEmbeddings.embed_query", traced_embedding(langchain))
        wrap("langchain", "embeddings.OpenAIEmbeddings.embed_documents", traced_embedding(langchain))
    # Text embedding models override two abstract base methods instead of super calls, so we need to
    #  wrap each langchain-provided text embedding model.
    for text_embedding_model in text_embedding_models:
        if hasattr(BASE_LANGCHAIN_MODULE.embeddings, text_embedding_model):
            # Ensure not double patched, as some Embeddings interfaces are pointers to other Embeddings.
            if not isinstance(
                deep_getattr(BASE_LANGCHAIN_MODULE.embeddings, "%s.embed_query" % text_embedding_model),
                wrapt.ObjectProxy,
            ):
                wrap(
                    BASE_LANGCHAIN_MODULE.__name__,
                    "embeddings.%s.embed_query" % text_embedding_model,
                    traced_embedding(langchain),
                )
            if not isinstance(
                deep_getattr(BASE_LANGCHAIN_MODULE.embeddings, "%s.embed_documents" % text_embedding_model),
                wrapt.ObjectProxy,
            ):
                wrap(
                    BASE_LANGCHAIN_MODULE.__name__,
                    "embeddings.%s.embed_documents" % text_embedding_model,
                    traced_embedding(langchain),
                )
    # We need to do the same with Vectorstores.
    for vectorstore in vectorstore_classes:
        if hasattr(BASE_LANGCHAIN_MODULE.vectorstores, vectorstore):
            # Ensure not double patched, as some Embeddings interfaces are pointers to other Embeddings.
            if not isinstance(
                deep_getattr(BASE_LANGCHAIN_MODULE.vectorstores, "%s.similarity_search" % vectorstore),
                wrapt.ObjectProxy,
            ):
                wrap(
                    BASE_LANGCHAIN_MODULE.__name__,
                    "vectorstores.%s.similarity_search" % vectorstore,
                    traced_similarity_search(langchain),
                )

    if _is_iast_enabled():
        from ddtrace.appsec._iast._metrics import _set_iast_error_metric

        def wrap_output_parser(module, parser):
            # Ensure not double patched
            if not isinstance(deep_getattr(module, "%s.parse" % parser), wrapt.ObjectProxy):
                wrap(module, "%s.parse" % parser, taint_parser_output)

        try:
            with_agent_output_parser(wrap_output_parser)
        except Exception as e:
            _set_iast_error_metric("IAST propagation error. langchain wrap_output_parser. {}".format(e))


def unpatch():
    if not getattr(langchain, "_datadog_patch", False):
        return

    if LLMObs.enabled:
        LLMObs.disable()

    langchain._datadog_patch = False

    if SHOULD_PATCH_LANGCHAIN_COMMUNITY:
        unwrap(langchain_core.language_models.llms.BaseLLM, "generate")
        unwrap(langchain_core.language_models.llms.BaseLLM, "agenerate")
        unwrap(langchain_core.language_models.chat_models.BaseChatModel, "generate")
        unwrap(langchain_core.language_models.chat_models.BaseChatModel, "agenerate")
        unwrap(langchain.chains.base.Chain, "invoke")
        unwrap(langchain.chains.base.Chain, "ainvoke")
        if langchain_openai:
            unwrap(langchain_openai.OpenAIEmbeddings, "embed_documents")
        if langchain_pinecone:
            unwrap(langchain_pinecone.PineconeVectorStore, "similarity_search")

    else:
        unwrap(langchain.llms.base.BaseLLM, "generate")
        unwrap(langchain.llms.base.BaseLLM, "agenerate")
        unwrap(langchain.chat_models.base.BaseChatModel, "generate")
        unwrap(langchain.chat_models.base.BaseChatModel, "agenerate")
        unwrap(langchain.chains.base.Chain, "__call__")
        unwrap(langchain.chains.base.Chain, "acall")
        unwrap(langchain.embeddings.OpenAIEmbeddings, "embed_query")
        unwrap(langchain.embeddings.OpenAIEmbeddings, "embed_documents")
    for text_embedding_model in text_embedding_models:
        if hasattr(BASE_LANGCHAIN_MODULE.embeddings, text_embedding_model):
            if isinstance(
                deep_getattr(BASE_LANGCHAIN_MODULE.embeddings, "%s.embed_query" % text_embedding_model),
                wrapt.ObjectProxy,
            ):
                unwrap(getattr(BASE_LANGCHAIN_MODULE.embeddings, text_embedding_model), "embed_query")
            if isinstance(
                deep_getattr(BASE_LANGCHAIN_MODULE.embeddings, "%s.embed_documents" % text_embedding_model),
                wrapt.ObjectProxy,
            ):
                unwrap(getattr(BASE_LANGCHAIN_MODULE.embeddings, text_embedding_model), "embed_documents")
    for vectorstore in vectorstore_classes:
        if hasattr(BASE_LANGCHAIN_MODULE.vectorstores, vectorstore):
            if isinstance(
                deep_getattr(BASE_LANGCHAIN_MODULE.vectorstores, "%s.similarity_search" % vectorstore),
                wrapt.ObjectProxy,
            ):
                unwrap(getattr(BASE_LANGCHAIN_MODULE.vectorstores, vectorstore), "similarity_search")

    delattr(langchain, "_datadog_integration")


def taint_outputs(instance, inputs, outputs):
    from ddtrace.appsec._iast._metrics import _set_iast_error_metric
    from ddtrace.appsec._iast._taint_tracking import get_tainted_ranges
    from ddtrace.appsec._iast._taint_tracking import taint_pyobject

    try:
        ranges = None
        for key in filter(lambda x: x in inputs, instance.input_keys):
            input_val = inputs.get(key)
            if input_val:
                ranges = get_tainted_ranges(input_val)
                if ranges:
                    break

        if ranges:
            source = ranges[0].source
            for key in filter(lambda x: x in outputs, instance.output_keys):
                output_value = outputs[key]
                outputs[key] = taint_pyobject(output_value, source.name, source.value, source.origin)
    except Exception as e:
        _set_iast_error_metric("IAST propagation error. langchain taint_outputs. {}".format(e))


def taint_parser_output(func, instance, args, kwargs):
    from ddtrace.appsec._iast._metrics import _set_iast_error_metric
    from ddtrace.appsec._iast._taint_tracking import get_tainted_ranges
    from ddtrace.appsec._iast._taint_tracking import taint_pyobject

    result = func(*args, **kwargs)
    try:
        try:
            from langchain_core.agents import AgentAction
            from langchain_core.agents import AgentFinish
        except ImportError:
            from langchain.agents import AgentAction
            from langchain.agents import AgentFinish
        ranges = get_tainted_ranges(args[0])
        if ranges:
            source = ranges[0].source
            if isinstance(result, AgentAction):
                result.tool_input = taint_pyobject(result.tool_input, source.name, source.value, source.origin)
            elif isinstance(result, AgentFinish) and "output" in result.return_values:
                values = result.return_values
                values["output"] = taint_pyobject(values["output"], source.name, source.value, source.origin)
    except Exception as e:
        _set_iast_error_metric("IAST propagation error. langchain taint_parser_output. {}".format(e))

    return result


def with_agent_output_parser(f):
    import langchain.agents

    queue = [(langchain.agents, agent_output_parser_classes)]

    while len(queue) > 0:
        module, current = queue.pop(0)
        if isinstance(current, str):
            if hasattr(module, current):
                f(module, current)
        elif isinstance(current, dict):
            for name, value in current.items():
                if hasattr(module, name):
                    queue.append((getattr(module, name), value))

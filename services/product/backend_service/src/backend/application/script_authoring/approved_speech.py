"""Approved authoring artifacts at the active pre-TTS boundary.

This deliberately uses an extractive Q&A contract: one complete approved
fact sentence, also present verbatim in the approved artifact. Arbitrary
paraphrases cannot be proven by the authoring gate's keyword overlap rules.
Unsupported output is silent; no additional human approval is requested.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import re
from typing import Any, Callable

from .fingerprints import ApprovalDependencies, approval_dependency_hash
from .models import Approval, ScriptVersion
from .runtime_handoff import resolve_approved_script
from .session_binding import RuntimePlan, validate_binding, _source_current_dependencies

CAPABILITY = "content.approved_speech.v1"


class SpeechRejected(ValueError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class ApprovedProduct:
    product_id: str
    approved_version_id: str
    spoken_text: str
    approval_hash: str
    facts_json: str

    def answers(self) -> tuple[str, ...]:
        # No substring fragments, negation removal, price templates or free
        # connectors. The complete claim must be an artifact sentence too.
        facts = json.loads(self.facts_json)
        claims = facts.get("allowed_claims", [])
        sentences = self.spoken_text.splitlines()
        sentences = [s for line in sentences for s in re.split(r"(?<=[.!?])\s+", line)]
        return tuple(c for c in claims if isinstance(c, str) and c in sentences and c.strip())


@dataclass(frozen=True)
class ExecutionEnvelope:
    session_id: str
    tenant_id: str
    business_session_id: str
    generation: str
    script_set_id: str
    brief_json: str
    products: tuple[ApprovedProduct, ...]

    @property
    def fingerprint(self) -> str:
        from dataclasses import asdict

        return _hash(_json(asdict(self)))

    def product(self, product_id: str | None) -> ApprovedProduct:
        for product in self.products:
            if product.product_id == product_id:
                return product
        raise SpeechRejected("unbound_product")

    def check_time(self) -> None:
        value = json.loads(self.brief_json).get("facts_valid_until")
        if not value:
            return
        try:
            deadline = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if deadline.tzinfo is None:
                raise ValueError("timezone required")
        except (ValueError, TypeError) as exc:
            raise SpeechRejected("invalid_fact_validity") from exc
        if datetime.now(timezone.utc) >= deadline:
            raise SpeechRejected("expired_facts")

    def catalog(self):
        from backend.application.entity.models import EntityDocument, KnowledgeBlock

        return [
            EntityDocument(
                id=p.product_id,
                entity_type="product",
                name=json.loads(p.facts_json).get("product_name") or p.product_id,
                knowledge_blocks=[KnowledgeBlock(id=p.approved_version_id, content=p.spoken_text)],
            )
            for p in self.products
        ]


@dataclass(frozen=True)
class ValidatedSpeech:
    envelope: ExecutionEnvelope
    product: ApprovedProduct
    text: str
    epoch: int
    route: str

    def evidence(self) -> dict:
        return {
            "policy": CAPABILITY,
            "script_set_id": self.envelope.script_set_id,
            "envelope_hash": self.envelope.fingerprint,
            "product_id": self.product.product_id,
            "approved_version_id": self.product.approved_version_id,
            "approval_hash": self.product.approval_hash,
            "text_sha256": _hash(self.text),
            "route": self.route,
        }


class ApprovedSpeech:
    def __init__(self, store: Any, source: Callable[[], Any]):
        self.store = store
        self.source = source
        self._pinned: dict[str, ExecutionEnvelope] = {}
        self._epochs: dict[str, int] = {}

    def cancel(self, session_id: str) -> None:
        self._epochs[session_id] = self._epochs.get(session_id, 0) + 1

    def rebind(self, session_id: str) -> None:
        self.cancel(session_id)
        self._pinned.pop(session_id, None)

    async def resolve(self, session_id: str) -> ExecutionEnvelope:
        meta = await self.store.get(session_id)
        if not meta or meta.get("status") not in ("active",):
            raise SpeechRejected("inactive_session")
        binding = meta.get("script_set_binding")
        source = self.source()
        if not isinstance(binding, dict) or not binding.get("script_set_id"):
            raise SpeechRejected("missing_binding")
        if source is None:
            raise SpeechRejected("approval_source_unavailable")
        entries = binding.get("products")
        if not isinstance(entries, list) or not entries:
            raise SpeechRejected("invalid_binding")
        try:
            product_ids = [entry["product_id"] for entry in entries]
            if len(set(product_ids)) != len(product_ids):
                raise SpeechRejected("invalid_binding")

            class Catalog:
                def contains(self, product_id):
                    return product_id in product_ids

            check = await validate_binding(
                script_set_id=binding["script_set_id"],
                source=source,
                runtime_plan=RuntimePlan(order_locked=False),
                runtime_catalog=Catalog(),
            )
            if not check.ok or check.script_set is None:
                raise SpeechRejected("missing_or_stale_approval")
            script_set = check.script_set
            if script_set.product_ids != product_ids:
                raise SpeechRejected("stale_binding")
            brief = script_set.brief
            identity = meta.get("execution_contract") or {}
            if not all((brief.tenant_id, brief.business_session_id, identity.get("generation"))):
                raise SpeechRejected("missing_execution_scope")
            if (
                identity.get("tenant_id"),
                identity.get("business_session_id"),
                identity.get("runtime_session_id"),
            ) != (brief.tenant_id, brief.business_session_id, session_id):
                raise SpeechRejected("execution_scope_mismatch")
            current = await _source_current_dependencies(source)
            products = []
            for entry in entries:
                product_id = entry["product_id"]
                approved = await resolve_approved_script(
                    source, script_set_id=script_set.id, product_id=product_id
                )
                if approved is None or (
                    approved.approved_version_id != entry["approved_version_id"]
                    or approved.spoken_text != entry["spoken_text"]
                    or not approved.spoken_text.strip()
                ):
                    raise SpeechRejected("stale_binding")
                version = await source.get_script_version(
                    set_id=script_set.id,
                    product_id=product_id,
                    version_id=approved.approved_version_id,
                )
                approval = await source.get_approval(
                    set_id=script_set.id,
                    product_id=product_id,
                    version_id=approved.approved_version_id,
                )
                version = ScriptVersion.model_validate(version)
                approval = Approval.model_validate(approval)
                expected_hash = approval_dependency_hash(
                    ApprovalDependencies(
                        spoken_text=version.spoken_text,
                        segment_hashes=tuple(version.segment_version_ids),
                        plan_version=version.plan_version,
                        rule_set=current.rule_set_version,
                        product_facts_version=brief.product_facts_version,
                        promotion_version=brief.promotion_version,
                        persona_brief_version=brief.persona_brief_version,
                    )
                )
                if (
                    approval.approval_hash != expected_hash
                    or not approval.actor
                    or not approval.gate_run_id
                    or version.spoken_text != approved.spoken_text
                ):
                    raise SpeechRejected("invalid_approval")
                facts = brief.product_facts.get(product_id)
                if not isinstance(facts, dict) or not brief.product_facts_version:
                    raise SpeechRejected("missing_approved_facts")
                products.append(
                    ApprovedProduct(
                        product_id,
                        approved.approved_version_id,
                        approved.spoken_text,
                        approval.approval_hash,
                        _json(facts),
                    )
                )
            envelope = ExecutionEnvelope(
                session_id,
                brief.tenant_id,
                brief.business_session_id,
                identity["generation"],
                script_set.id,
                brief.model_dump_json(),
                tuple(products),
            )
            envelope.check_time()
            pinned = self._pinned.setdefault(session_id, envelope)
            if pinned != envelope:
                raise SpeechRejected("stale_execution_envelope")
            return pinned
        except (KeyError, TypeError, ValueError) as exc:
            if isinstance(exc, SpeechRejected):
                raise
            raise SpeechRejected("invalid_binding") from exc

    async def prepare(
        self,
        session_id: str,
        text: str,
        *,
        llm: Any = None,
        generate: bool = False,
        product_id: str | None = None,
        select_locked: bool = False,
        route: str,
        live: Callable[[], bool] = lambda: True,
    ) -> ValidatedSpeech:
        epoch = self._epochs.get(session_id, 0)
        envelope = await self.resolve(session_id)
        candidates = (envelope.product(product_id),) if product_id else envelope.products
        if select_locked:
            text = envelope.product(product_id).spoken_text
        elif generate:
            if llm is None or getattr(llm, "name", "none") == "none":
                raise SpeechRejected("generation_unavailable")
            from llm.engines.base import LLMRequest

            answers = [answer for p in candidates for answer in p.answers()]
            if not answers:
                raise SpeechRejected("no_approved_answer")
            prompt = _json({"question": text[:4000], "approved_answers": answers})
            request = LLMRequest.from_prompt(
                prompt,
                system_prompt=(
                    "Select exactly one complete approved_answers string that answers the question. "
                    "Return it byte for byte, without additions. If unsupported return an empty string."
                ),
            )

            def collect():
                parts = []
                size = 0
                for chunk in llm.stream_chunks(request, session_id=session_id):
                    if epoch != self._epochs.get(session_id, 0) or not live():
                        raise SpeechRejected("cancelled_speech")
                    size += len(chunk.text)
                    if size > 16000:
                        raise SpeechRejected("generated_unit_too_large")
                    parts.append(chunk.text)
                return "".join(parts)

            text = await asyncio.to_thread(collect)
        product = next(
            (p for p in candidates if (text == p.spoken_text or text in p.answers())), None
        )
        if product is None:
            raise SpeechRejected("unsupported_content")
        speech = ValidatedSpeech(envelope, product, text, epoch, route)
        await self.revalidate(speech, live=live)
        return speech

    def check_live(self, speech: ValidatedSpeech, live: Callable[[], bool]) -> None:
        if speech.epoch != self._epochs.get(speech.envelope.session_id, 0) or not live():
            raise SpeechRejected("cancelled_speech")
        speech.envelope.check_time()

    async def revalidate(self, speech: ValidatedSpeech, *, live=lambda: True) -> None:
        self.check_live(speech, live)
        if await self.resolve(speech.envelope.session_id) != speech.envelope:
            raise SpeechRejected("stale_execution_envelope")
        self.check_live(speech, live)

    def guarded_tts(self, speech, tts, *, live, emit):
        """Recheck on the event loop before each actual TTS chunk dispatch."""
        service = self
        loop = asyncio.get_running_loop()

        class GuardedTTS:
            def stream_audio(self, chunk, **kwargs):
                async def before_tts():
                    await service.revalidate(speech, live=live)
                    if chunk.text not in speech.text:
                        raise SpeechRejected("altered_approved_chunk")
                    await emit(
                        speech.envelope.session_id,
                        {
                            "type": "speech.content_validated",
                            **speech.evidence(),
                            "utterance_id": kwargs.get("utterance_id"),
                            "chunk_id": chunk.id,
                            "chunk_sha256": _hash(chunk.text),
                        },
                    )
                    service.check_live(speech, live)

                asyncio.run_coroutine_threadsafe(before_tts(), loop).result()
                service.check_live(speech, live)
                for window in tts.stream_audio(chunk, **kwargs):
                    service.check_live(speech, live)
                    yield window

        return GuardedTTS()

    def guarded_audio(self, speech, *, live, callback=None):
        async def publish(window):
            # Provider work can finish after cancellation/expiry. This also
            # covers a pending window flushed by the chunker's error path.
            await self.revalidate(speech, live=live)
            if callback is not None:
                await callback(window)

        return publish

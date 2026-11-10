import hashlib
import json
import uuid

from django.conf import settings
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from apps.credits.models import CreditTransaction, Wallet, settle_reservation
from apps.workshop.models import Message, Project, Revision

from .models import AIJob


class JobError(Exception):
    code = "job_error"


class InsufficientCredits(JobError):
    code = "insufficient_credits"


class JobConflict(JobError):
    code = "job_conflict"


class QueueFull(JobError):
    code = "queue_full"


class EditScopeError(JobError):
    code = "edit_scope_violation"


def canonical_hash(payload):
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode()).hexdigest()


def create_job(*, owner, kind, idempotency_key, instruction="", project=None, base_revision=None, target_id=""):
    """Create a paid job with credit reservation in one DB transaction."""
    input_json = {"instruction": instruction.strip()[: settings.MAX_EDIT_INSTRUCTION_CHARS if kind in (AIJob.Kind.EDIT_SENTENCE, AIJob.Kind.EDIT_PART, AIJob.Kind.EDIT_SERIES) else settings.MAX_USER_PROMPT_CHARS], "kind": kind}
    if target_id:
        input_json["target_id"] = target_id
    if base_revision is not None:
        input_json["base_revision_id"] = str(base_revision.pk)
    input_hash = canonical_hash(input_json)
    amount = settings.CREDIT_COSTS[kind]
    with transaction.atomic():
        existing = AIJob.objects.select_for_update().filter(owner=owner, idempotency_key=idempotency_key).first()
        if existing:
            if existing.input_hash != input_hash:
                raise JobConflict("Этот ключ уже использован для другого запроса")
            return existing, False
        active = AIJob.objects.select_for_update().filter(owner=owner, status__in=[AIJob.Status.QUEUED, AIJob.Status.RUNNING]).count()
        if active >= settings.USER_ACTIVE_AI_JOBS:
            raise JobConflict("Дождитесь завершения текущей задачи")
        if AIJob.objects.filter(status=AIJob.Status.QUEUED).count() >= settings.GLOBAL_QUEUE_CAPACITY:
            raise QueueFull("Очередь временно заполнена")
        if project and project.owner_id != owner.pk:
            raise JobConflict("Проект не найден")
        wallet, _ = Wallet.objects.select_for_update().get_or_create(user=owner)
        if wallet.available_credits < amount:
            raise InsufficientCredits("Недостаточно кредитов для этого действия")
        job = AIJob.objects.create(
            owner=owner,
            kind=kind,
            project=project,
            base_revision=base_revision,
            input_json=input_json,
            input_hash=input_hash,
            idempotency_key=idempotency_key,
            reserved_credits=amount,
            policy_version=settings.CREDIT_POLICY_VERSION,
        )
        Wallet.objects.filter(pk=wallet.pk).update(reserved_credits=wallet.reserved_credits + amount)
        from apps.credits.models import CreditReservation

        CreditReservation.objects.create(job=job, wallet=wallet, amount=amount)
        CreditTransaction.objects.create(
            wallet=wallet,
            kind=CreditTransaction.Kind.RESERVE,
            signed_amount=-amount,
            operation_key=f"reserve:{job.pk}",
            job=job,
            reason="Резерв для задачи ИИ",
        )
        if project:
            Message.objects.create(project=project, owner=owner, role="user", kind=kind, body=instruction, job=job)
        return job, True


def get_provider():
    if settings.AI_PROVIDER == "fake" and not settings.LIVE_AI_ENABLED:
        return FakeProvider()
    if settings.LIVE_AI_ENABLED and settings.AI_PROVIDER == "openrouter":
        from .openrouter import OpenRouterProvider

        return OpenRouterProvider()
    raise JobError("Живая генерация не настроена")


class FakeProvider:
    """Explicit development/test provider. Not a philosophical quality check."""

    def generate(self, job):
        instruction = job.input_json.get("instruction", "")
        return {
            "outcome": AIJob.Outcome.DELIVERED,
            "payload": {
                "title_en": "Your question",
                "title_ru": "Черновик по вашему запросу",
                "note": "Тестовый результат FakeProvider, не ответ живой модели",
                "parts": [
                    {
                        "title_en": "Your question",
                        "sentences": [
                            {"en": "You asked about a philosophical idea.", "ru": "Вы спросили о философской идее."},
                            {"en": instruction[:300] or "The request is empty.", "ru": "Ваш запрос сохранён для дальнейшей настройки живой модели."},
                        ],
                    }
                ],
                "terms": [],
                "glossary": [],
            },
            "answer": f"Тестовый ответ FakeProvider по запросу: {instruction[:500]}",
        }

    def edit(self, job, context=None):
        """Deterministic edit decisions for dev/test only."""
        target = job.input_json.get("target_id", "")
        base = job.base_revision
        parts = base.payload.get("parts", [])
        try:
            part_idx, sentence_idx = (int(piece) for piece in target.split(":", 1))
        except (ValueError, AttributeError):
            return {"outcome": "needs_broader_edit", "message_ru": "Цель правки не распознана"}
        instruction = job.input_json.get("instruction", "").strip().lower()
        if "broader" in instruction:
            return {"outcome": "needs_broader_edit", "message_ru": "Правка требует изменения соседних частей"}
        if job.kind == AIJob.Kind.EDIT_SENTENCE:
            replacement = {"en": "Edited sentence (test provider).", "ru": "Исправленное предложение (тестовый провайдер)."}
            return {"outcome": "edited", "replacement_sentences": [replacement]}
        part = parts[part_idx]
        new_sentences = [{"en": "Edited part for testing (test provider).", "ru": "Исправленная часть для теста (тестовый провайдер)."}]
        return {"outcome": "edited", "part": {"title_en": part.get("title_en", ""), "sentences": new_sentences}}

    def review(self, payload, job=None):
        """Deterministic review for dev/test: pass unless flagged. The
        instruction propagates into the generated payload, so flags are
        recognizable there."""
        instruction = (job.input_json.get("instruction", "") if job else "") or ""
        if "fail-review" in instruction:
            return {"verdict": "fail", "factual_state": "unsupported", "issues": [{"code": "unsupported", "severity": "blocking", "reason_ru": "Тестовая блокирующая проблема"}]}
        if "repair-review" in instruction and not any("repaired" in json.dumps(part, ensure_ascii=False) for part in payload.get("parts", [])):
            return {"verdict": "repair", "factual_state": "mixed", "issues": [{"code": "fixable", "severity": "blocking", "reason_ru": "Тестовая исправимая проблема"}]}
        return {"verdict": "pass", "factual_state": "unverified", "issues": []}

    def repair(self, payload, issues, job=None):
        """Deterministic repair for dev/test: replaces the flagged marker
        text so the re-review passes."""
        if not payload:
            return None
        repaired = json.loads(json.dumps(payload))  # deep copy
        for part in repaired.get("parts", []):
            for sentence in part.get("sentences", []):
                for key in ("en", "ru"):
                    value = sentence.get(key, "")
                    if "repair-review" in value:
                        sentence[key] = value.replace("repair-review", "repaired")
                    if "fail-review" in value:
                        sentence[key] = value.replace("fail-review", "repaired")
        return repaired


def claim_next_job():
    with transaction.atomic():
        job = AIJob.objects.select_for_update(skip_locked=True).filter(status=AIJob.Status.QUEUED).order_by("created_at").first()
        if not job:
            return None
        job.status = AIJob.Status.RUNNING
        job.lease_token = uuid.uuid4().hex
        job.lease_until = timezone.now() + timezone.timedelta(seconds=settings.AI_LEASE_SECONDS)
        job.save(update_fields=["status", "lease_token", "lease_until", "updated_at"])
        return job


def run_review_cycle(provider, job, result):
    """One review -> (one repair -> fresh review) cycle, shared by providers.

    The candidate survives only when the final review passes; otherwise the
    outcome becomes no_charge_failure with a saved free explanation. Calls
    are capped by AI_MAX_PROVIDER_CALLS_PER_JOB (generate/edit -> review ->
    repair -> review = 4).
    """
    review = provider.review(result["payload"], job)
    if review.get("verdict") == "pass":
        result["review"] = {"passed": True, "review": review}
        return result
    if review.get("verdict") != "repair" or not hasattr(provider, "repair"):
        result["review"] = {"passed": False, "review": review, "message_ru": _review_failure_message(review)}
        return result
    repaired = provider.repair(result["payload"], review.get("issues", []), job)
    if repaired is None:
        result["review"] = {"passed": False, "review": review, "message_ru": "Не удалось исправить кандидата"}
        return result
    re_review = provider.review(repaired, job)
    if re_review.get("verdict") == "pass":
        result["payload"] = repaired
        result["review"] = {"passed": True, "review": re_review}
    else:
        result["review"] = {"passed": False, "review": re_review, "message_ru": _review_failure_message(re_review)}
    return result


def _review_failure_message(review):
    issues = review.get("issues") or []
    for issue in issues:
        if issue.get("severity") == "blocking":
            return f"Проверка не пройдена: {issue.get('reason_ru', 'содержательные проблемы')}"[:500]
    return "Проверка качества не пройдена; результат не сохранён как полезный текст"


def _build_edit_context(job):
    """Server-built fragment context from the immutable base revision.

    The model never receives scope information from user input: the target,
    its text and neighbours come from the stored snapshot only (spec
    fragment-editing.md: operation, base revision and target are set by the
    server)."""
    base = job.base_revision
    if base is None:
        raise EditScopeError("Базовая ревизия недоступна")
    payload = base.payload
    parts = payload.get("parts", [])
    part_idx, sentence_idx = _validate_edit_target(base, job)
    part = parts[part_idx]
    context = {
        "series_title_en": payload.get("title_en", ""),
        "series_title_ru": payload.get("title_ru", ""),
        "part_title_en": part.get("title_en", ""),
    }
    sentences = part.get("sentences", [])
    if job.kind == AIJob.Kind.EDIT_PART:
        context["target"] = "part"
        context["part_text"] = [
            {"en": s.get("en", "")[:600], "ru": s.get("ru", "")[:600]} for s in sentences[:12]
        ]
        return context
    context["target"] = "sentence"
    if sentence_idx > 0:
        prev = sentences[sentence_idx - 1]
        context["previous_sentence"] = {"en": prev.get("en", "")[:600], "ru": prev.get("ru", "")[:600]}
    target = sentences[sentence_idx]
    context["target_sentence"] = {"en": target.get("en", "")[:600], "ru": target.get("ru", "")[:600]}
    if sentence_idx + 1 < len(sentences):
        nxt = sentences[sentence_idx + 1]
        context["next_sentence"] = {"en": nxt.get("en", "")[:600], "ru": nxt.get("ru", "")[:600]}
    return context


def execute_job(job):
    provider = get_provider()
    is_edit = job.kind in (AIJob.Kind.EDIT_SENTENCE, AIJob.Kind.EDIT_PART, AIJob.Kind.EDIT_SERIES)
    try:
        # Network happens here, strictly outside DB transactions.
        if is_edit:
            decision = provider.edit(job, context=_build_edit_context(job))
            result = apply_edit(job, decision)
            if result["outcome"] == AIJob.Outcome.DELIVERED:
                result = run_review_cycle(provider, job, result)
                if not result["review"]["passed"]:
                    result["outcome"] = AIJob.Outcome.NO_CHARGE_FAILURE
                    result["payload"] = {}
        else:
            result = provider.generate(job)
            if result["outcome"] == AIJob.Outcome.DELIVERED:
                result = run_review_cycle(provider, job, result)
                if not result["review"]["passed"]:
                    result["outcome"] = AIJob.Outcome.NO_CHARGE_FAILURE
                    result["payload"] = {}
    except Exception as exc:
        with transaction.atomic():
            settle_reservation(job=job, charge=False)
            job.status = AIJob.Status.FAILED
            job.outcome = None
            job.error_code = getattr(exc, "code", "provider_unavailable") if not isinstance(exc, JobError) else "ai_not_configured"
            job.result_reference = {"message": str(exc)[:300]}
            job.completed_at = timezone.now()
            job.save()
        return job
    attempts = getattr(provider, "_attempts", None) or ([getattr(provider, "_last_attempt", None)] if getattr(provider, "_last_attempt", None) else [])
    with transaction.atomic():
        outcome = result["outcome"]
        charge = outcome == AIJob.Outcome.DELIVERED
        if outcome == AIJob.Outcome.BROADER_EDIT or outcome == AIJob.Outcome.CLARIFICATION or outcome == AIJob.Outcome.REFUSED:
            # Delivered no-charge outcomes: save the free explanation, no
            # revision snapshot, no debit (spec: needs_broader_edit etc.).
            Message.objects.create(
                project=job.project,
                owner=job.owner,
                role="assistant",
                kind="explanation",
                body=result.get("answer") or "Изменение не выполнено",
                job=job,
            )
            job.result_reference = {"explanation": "saved"}
        elif outcome == AIJob.Outcome.NO_CHARGE_FAILURE:
            # Spec: a failed review may end as no_charge_failure only with a
            # saved free explanation; it is not a technical failure.
            Message.objects.create(
                project=job.project,
                owner=job.owner,
                role="assistant",
                kind="review",
                body=(result.get("review", {}) or {}).get("message_ru") or "Проверка качества не пройдена",
                job=job,
            )
            job.result_reference = {"review": "failed"}
        elif job.kind == AIJob.Kind.ASK:
            Message.objects.create(project=job.project, owner=job.owner, role="assistant", kind="answer", body=result["answer"], job=job)
            job.result_reference = {"message_id": "answer"}
        else:
            revision = Revision.objects.create(project=job.project, parent=job.base_revision, payload=result["payload"], created_by_job=job)
            # Spec: a successful first series sets head automatically; a later
            # edit only adds a revision — head moves after explicit "apply".
            if job.kind == AIJob.Kind.GENERATE:
                job.project.head_revision = revision
                job.project.save(update_fields=["head_revision"])
            job.result_reference = {"revision_id": str(revision.pk)}
            review = result.get("review") or {}
            if review and job.kind == AIJob.Kind.GENERATE:
                from .models import QualityReport

                QualityReport.objects.create(
                    revision=revision,
                    revision_content_hash=revision.content_hash,
                    deterministic_json={},
                    review_json=review.get("review", {}),
                    review_model=attempts[-1]["model"] if attempts else "",
                    prompt_version="review-v2",
                    schema_version="review-1",
                    factual_state=review.get("review", {}).get("factual_state", "unverified"),
                    eligibility_state="ineligible",
                )
        if attempts:
            from .models import ProviderAttempt

            for stage_number, attempt in enumerate(attempts, start=1):
                ProviderAttempt.objects.create(
                    job=job,
                    stage_number=stage_number,
                    request_hash=attempt["request_hash"],
                    state="completed",
                    provider_request_id=attempt["provider_request_id"] or "",
                    model=attempt["model"],
                    provider="openrouter",
                    usage_json=attempt["usage_json"],
                    cost_usd=result.get("cost_usd") if stage_number == 1 else None,
                    sent_at=timezone.now(),
                    completed_at=timezone.now(),
                )
        settle_reservation(job=job, charge=charge)
        job.status = AIJob.Status.SUCCEEDED
        job.outcome = outcome
        job.stage = "finalize"
        job.completed_at = timezone.now()
        job.save()
    return job


# --- fragment edits (stage 7) ------------------------------------------------


def _validate_edit_target(base_revision, job):
    """Resolve the edited fragment from input_json.target_id inside the base
    revision. Returns (part_index, sentence_index|None). For edit_part the
    target is a bare part index; for edit_sentence "part:sentence"."""
    target = job.input_json.get("target_id", "")
    parts = base_revision.payload.get("parts", [])
    pieces = target.split(":", 1) if isinstance(target, str) else []
    if job.kind == AIJob.Kind.EDIT_PART:
        try:
            part_idx = int(pieces[0])
        except (ValueError, IndexError):
            raise EditScopeError("Цель правки задана некорректно")
        if not 0 <= part_idx < len(parts):
            raise EditScopeError("Цель вне области ревизии")
        return part_idx, None
    if len(pieces) != 2:
        raise EditScopeError("Цель правки задана некорректно")
    try:
        part_idx, sentence_idx = int(pieces[0]), int(pieces[1])
    except ValueError:
        raise EditScopeError("Цель правки задана некорректно")
    if not 0 <= part_idx < len(parts):
        raise EditScopeError("Цель вне области ревизии")
    sentences = parts[part_idx].get("sentences", [])
    if not 0 <= sentence_idx < len(sentences):
        raise EditScopeError("Предложение вне области части")
    return part_idx, sentence_idx


def apply_edit(job, decision):
    """Validate the edit scope and assemble the new full snapshot.

    The server, never the model, decides what may change: sentence edits
    replace one sentence with one or two; part edits replace only that
    part's content. Unchanged sentences/translations are copied verbatim
    from the base revision; titles and glossary stay untouched by a
    sentence edit. Any mismatch is rejected instead of silently applied.
    """
    base_revision = job.base_revision
    part_idx, sentence_idx = _validate_edit_target(base_revision, job)
    outcome = decision.get("outcome")

    if outcome == "needs_broader_edit":
        return {"outcome": AIJob.Outcome.BROADER_EDIT, "payload": {}, "answer": decision.get("message_ru", "")}
    if outcome == "needs_clarification":
        return {"outcome": AIJob.Outcome.CLARIFICATION, "payload": {}, "answer": decision.get("message_ru", "")}
    if outcome == "refused":
        return {"outcome": AIJob.Outcome.REFUSED, "payload": {}, "answer": decision.get("message_ru", "")}
    if outcome != "edited":
        raise EditScopeError("Неизвестный исход правки")

    parts = [dict(part) for part in base_revision.payload.get("parts", [])]
    payload_out = dict(base_revision.payload)
    if job.kind == AIJob.Kind.EDIT_SENTENCE:
        replacements = decision.get("replacement_sentences") or []
        if not 1 <= len(replacements) <= 2:
            raise EditScopeError("Модель вернула недопустимое число предложений")
        for item in replacements:
            if not item.get("en", "").strip() or not item.get("ru", "").strip():
                raise EditScopeError("Пустая замена предложения")
            if len(item["en"]) > 600 or len(item["ru"]) > 600:
                raise EditScopeError("Предложение замены слишком длинное")
        sentences = list(parts[part_idx].get("sentences", []))
        parts[part_idx]["sentences"] = (
            sentences[:sentence_idx] + replacements + sentences[sentence_idx + 1:]
        )
    else:  # edit_part: the model returns the full replacement of one part
        part = decision.get("part") or {}
        sentences = part.get("sentences") or []
        if not 1 <= len(sentences) <= 12:
            raise EditScopeError("Недопустимое число предложений в части")
        if any(not item.get("en", "").strip() or not item.get("ru", "").strip() for item in sentences):
            raise EditScopeError("Пустое предложение в части")
        new_part = dict(parts[part_idx])
        new_part["title_en"] = parts[part_idx].get("title_en", "")
        new_part["sentences"] = [{"en": item["en"], "ru": item["ru"]} for item in sentences]
        parts[part_idx] = new_part
    payload_out["parts"] = parts
    return {"outcome": AIJob.Outcome.DELIVERED, "payload": payload_out, "answer": ""}


def recover_stale_jobs():
    cutoff = timezone.now()
    stale = AIJob.objects.filter(status=AIJob.Status.RUNNING, lease_until__lt=cutoff)
    count = 0
    for job in stale:
        with transaction.atomic():
            locked = AIJob.objects.select_for_update().get(pk=job.pk)
            if locked.status != AIJob.Status.RUNNING or locked.lease_until >= cutoff:
                continue
            settle_reservation(job=locked, charge=False)
            locked.status = AIJob.Status.FAILED
            locked.error_code = "lease_expired"
            locked.completed_at = cutoff
            locked.save()
            count += 1
    return count

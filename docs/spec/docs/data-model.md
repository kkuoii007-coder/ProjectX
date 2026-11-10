# Модель данных

## Общие правила

UUID для пользовательских сущностей, UTC для времени.
Внешний UUID не заменяет проверку прав.
Кредиты — целые числа. Деньги — Decimal с фиксированной точностью.
Персональные данные не помещаются в публичные slug.

## Аккаунты

User: id, login_normalized unique, password hash, public_alias,
is_active, is_staff, created_at, auth_epoch, legal_ack_version.

Custom User строить без обязательных email, first_name и last_name.
Пароли хранятся средствами Django, никогда как исходная строка.

UserSession: user, session_key_reference, auth_epoch, created_at, expires_at.
Не хранить сырые session keys в отдельном журнале.

## Библиотека

Thinker: slug, name_en, name_ru.
Topic: slug, name_en, name_ru.
Collection и CollectionItem: упорядоченная редакционная подборка.

KnowledgeCard: slug, version, title, facts_json, pitfalls_json,
verification_status draft/verified/retired, verified_at, verified_by.
Unique(slug, version).
Каждый факт карточки имеет стабильный fact_id, формулировку, тип, ограничения
упрощения и ссылки на SourceChunk. Карточки версионируются: изменение карточки
не переписывает историю проверок. RAG не заменяет редакционную карточку.

Publication: slug unique, revision, quality_report, source_type editorial,
status published/hidden/withdrawn, published_at, published_by nullable.
Публичный текст — ссылка на неизменяемую ревизию, а не на project.head.
quality_report фиксирует отчёт, на основании которого администратор опубликовал
снимок; права на источники сервер повторно проверяет в момент публикации.
source_type=community и submission добавляются следующим этапом.

## Источники и RAG

SourceWork: автор, название, тип primary/secondary, язык, канонический URL nullable.
SourceEdition: work, издание и переводчик, язык, права, URL, retrieved_at.
SourceDocument: owner nullable, edition nullable, lineage_id, version,
supersedes nullable, origin user/editorial/web, visibility private/editorial,
rights_state unknown/allowed/private_only/prohibited,
verification_state unverified/verified/rejected, content_hash,
storage_reference nullable, normalized_text,
processing_state uploaded/extracting/indexing/ready/failed, metadata_json,
created_at. Unique(lineage_id, version), Unique(lineage_id, content_hash).
Найденный web-документ не получает verification автоматически. Версия документа —
отдельная строка; supersedes указывает на предыдущую версию, не перезаписывая её.
Все версии lineage имеют одинаковые owner и visibility. Retrieval использует ready.

SourceChunk: document, ordinal, text, locator_json, token_count,
search_vector, content_hash. Unique(document, ordinal).
Embedding: source_chunk, model, dimensions, vector, content_hash, created_at.
Unique(source_chunk, model, dimensions, content_hash).
Активная версия embedding выбирается конфигурацией индекса. Доступ наследуется
от document и проверяется в каждом retrieval. Embeddings не делают приватный
текст публичным; при удалении документа удаляются и его embeddings.

SourceSearch: owner nullable, query, provider, allowlist_version, results_json,
created_at. SearchResultSelection: search, selected_by, url, document nullable.
Все выбранные URL проходят отдельную серверную проверку перед fetch.

## Мастерская

Project: owner, title, mode simplify_source/explain_topic, head_revision nullable,
created_at.
ProjectSource: project, document, role primary/context, added_by.
Revision: project, parent nullable, payload JSON, content_hash,
created_by_job nullable unique, created_at.
RevisionKnowledgeCard: revision, knowledge_card; связь фиксирует конкретную
версию карточки, а не только её slug.
RevisionSource: revision, document nullable, provenance_json, role; document
указывает на конкретную версию SourceDocument, включённую в снимок. После удаления
источника сохраняются только безопасные provenance-метаданные, а не его текст.
Payload и content_hash ревизии неизменяемы. Языковая проверка, фактическая опора и
допуск к публикации — производные оценки, не поля неизменяемого payload.
factual_state: unverified/supported/mixed/unsupported;
publication_eligibility: ineligible/eligible вычисляется по актуальному QualityReport
и текущим правам. Для eligible обязательны verified-карточки, supported factual_state,
покрытые ключевые утверждения, доступные источники и допустимые права.

Payload: заголовки, упорядоченные части, пары предложений en/ru,
термины, русское пояснение упрощений. Порядок — явное поле.
EvidenceLink: revision, part/sentence locator, kind claim_support/source_alignment,
claim_text nullable, citation_id, fact_id nullable, source_chunk nullable,
locator_json, support_type quotation/paraphrase/interpretation.
Для simplify_source source_alignment связывает часть с исходным диапазоном;
claim_support сохраняет ссылки на evidence и разрешённые факты карточки.
При удалении SourceChunk исчезает доказательная связь, а не появляется копия текста.
Серверные citation_id существуют внутри действия и сверяются с RetrievalItem.

Message: project, owner, role, kind, body, job nullable, created_at.
Системный prompt хранить отдельно от пользовательского диалога.

Favorite: unique(user, publication).
SavedWord: user, term, normalized_term, translation_ru, context_en,
source_reference nullable. Уникальность определяется пользователем и term;
контекст допускается обновлять явным действием.

## ИИ

AIJob: owner, project nullable, document nullable, kind, base_revision,
input_json, input_hash, idempotency_key, status, stage, outcome nullable,
reserved_credits, policy_version, lease_until, lease_token, result_reference,
error_code, timestamps. Unique(owner, idempotency_key).
kind: generate/edit_series/edit_part/edit_sentence/ask/import_source/index_source/
search_sources.
outcome: delivered/needs_clarification/refused/needs_broader_edit/no_charge_failure.
status=succeeded означает завершение и доставку результата, не обязательно списание.
no_charge_failure допустим только с сохранённым бесплатным объяснением; необработанная
техническая задача имеет status=failed и не маскируется под содержательный отказ.

RetrievalItem: job, source_chunk, citation_id, rank, score_json.
SourceAttempt: job, stage_number, operation search/fetch/embed/index, request_hash,
state, provider, usage_json, cost_usd nullable, error_code, timestamps.
Уникальный stage_number не используется повторно для сетевого вызова.

ProviderAttempt: job, stage_number, request_hash, state,
provider_request_id nullable, model, provider nullable,
pricing_snapshot, usage_json, cost_usd nullable, max_reserved_usd,
sent_at, completed_at. Unique(job, stage_number).

QualityReport: revision, revision_content_hash, deterministic_json, review_json,
review_model, prompt_version, schema_version, retrieval_snapshot_hash,
card_versions_json, factual_state, eligibility_state, created_at.
Языковые результаты хранятся в deterministic_json/review_json. Отчёт фиксирует
source document version IDs и hashes в retrieval_snapshot_hash, а не только запрос.
При изменении текста, источников, карточек, значимой конфигурации или схемы старый
отчёт не подтверждает новый кандидат. pass не означает экспертную истинность.

## Кредитный учёт

Wallet: user unique, total_credits, reserved_credits.
Constraints: total_credits >= 0, reserved_credits >= 0,
reserved_credits <= total_credits.

CreditTransaction: wallet, kind, signed_amount, operation_key unique,
job nullable, reason, created_at. Записи неизменяемы.
Резервы отражаются отдельной CreditReservation:
job unique, amount, state active/captured/released.

CreditGrant: user, campaign, amount, transaction.
Unique(user, campaign). Первый campaign — welcome-v1.

BudgetBucket: period, limit_usd, consumed_usd, reserved_usd.
BudgetReservation: job/attempt, amount, state.
Неизвестную стоимость не превращать в ноль.

Payment: provider, external_id unique, owner, pack_snapshot,
amount, currency, state. Только при включении продаж.

## Жалобы и аудит

Report: reporter nullable, publication, category, details, status, created_at.
Жалобы на публичные материалы входят в MVP; автоматического сокрытия по одной
жалобе нет. Связь с submission добавляется следующим этапом.
AuditEvent: actor_reference nullable, action, object_reference,
reason, sanitized_metadata, timestamp.

## Сообщество — следующий этап, не MVP

Submission: owner, revision unique, status, candidate_since,
policy_version, blocking_hold_reason nullable.
Vote: user, submission, score 1..5, eligible, created_at.
Unique(user, submission). Автор исключён серверной проверкой.
Эти модели не создаются заранее в MVP; Publication.submission и новый source_type
добавляются миграцией следующего этапа.

## Удаление

Подробная политика в auth-privacy-security.md.
Не использовать слепой CASCADE на внешние финансовые события или аудит.
Не сохранять полный аккаунт бессрочно под видом soft delete.

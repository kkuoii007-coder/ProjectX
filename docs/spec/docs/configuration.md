# Конфигурация

## Правила

Все defaults централизованы, не размножаются по шаблонам.
Секреты через environment или защищённый файл вне Git.
Денежные лимиты production не получают придуманного «безопасного» значения.

## Основные параметры

| Параметр | Default |
|---|---|
| UI_LANGUAGE | ru |
| TEXT_LANGUAGE | en |
| TRANSLATION_LANGUAGE | ru |
| WELCOME_CREDITS | 30 |
| WELCOME_CAMPAIGN | welcome-v1 |
| COST_GENERATE_SERIES | 10 |
| COST_EDIT_SERIES | 10 |
| COST_EDIT_PART | 3 |
| COST_EDIT_SENTENCE | 1 |
| COST_ASK | 1 |
| TOPUP_MODE | manual_test |
| LIVE_AI_ENABLED | false |
| REGISTRATION_MODE | closed |
| USER_ACTIVE_AI_JOBS | 1 |
| AI_WORKER_CONCURRENCY | 1 |
| MAX_PROVIDER_CALLS_PER_JOB | 4; для ask применяется отдельный жёсткий лимит 1 |
| AI_HTTP_TIMEOUT_SECONDS | 120 |
| AI_UNRESOLVED_USER_TIMEOUT_MINUTES | 15 |
| AI_CONTEXT_MAX_MESSAGES | 8 |
| USER_AI_REQUESTS_PER_HOUR | 20 |
| GLOBAL_QUEUE_CAPACITY | 50 |
| MAX_USER_PROMPT_CHARS | 2000 |
| MAX_EDIT_INSTRUCTION_CHARS | 1000 |
| WEB_SEARCH_ENABLED | false |
| WEB_FETCH_ENABLED | false |
| SEARCH_PROVIDER | не задан |
| SOURCE_DOMAIN_ALLOWLIST | пустой до настройки |
| SEARCH_RESULTS_MAX | 5 |
| SOURCE_FETCH_MAX_BYTES | 5 МБ |
| SOURCE_FETCH_TIMEOUT_SECONDS | 20 |
| SOURCE_FETCH_MAX_REDIRECTS | 3 |
| SOURCE_UPLOAD_MAX_BYTES | 5 МБ |
| SOURCE_MAX_PAGES | 150 |
| USER_ACTIVE_DOCUMENTS_MAX | 10 |
| SOURCE_CHUNK_WORDS_MIN | 300 |
| SOURCE_CHUNK_WORDS_MAX | 600 |
| RETRIEVAL_MAX_CHUNKS | 8 |
| RETRIEVAL_MAX_INPUT_TOKENS | серверный cap выбранной модели |
| COMMUNITY_ENABLED | false; следующий этап |
| REPORTS_PER_USER_PER_DAY | 3 |
| LOGIN_ATTEMPTS_PER_ACCOUNT_15_MIN | 10 |
| LOGIN_ATTEMPTS_PER_NETWORK_15_MIN | 30 |
| REGISTRATIONS_PER_NETWORK_PER_DAY | 3 |
| REGISTRATIONS_GLOBAL_PER_DAY | 50 |

COMMUNITY_ENABLED зарезервирован для следующего этапа и не включает сообщество
в MVP. Пороги голосования хранятся как defaults в voting-and-publication.md,
а не в активных MVP settings.

Лимиты не доказывают отсутствие злоупотреблений.
На общем IP возможны легитимные пользователи: показывать понятный отказ.

## Обязательные настройки живого ИИ

OPENROUTER_API_KEY,
AI_GENERATION_MODEL,
AI_REVIEW_MODEL,
AI_EMBEDDING_MODEL,
AI_EMBEDDING_DIMENSIONS,
AI_PROVIDER_ALLOWLIST,
AI_PRIVACY_POLICY,
AI_MAX_INPUT_TOKENS,
AI_MAX_OUTPUT_TOKENS,
AI_STAGE_PRICE_CEILINGS,
OWNER_DAILY_BUDGET_USD,
OWNER_MONTHLY_BUDGET_USD.

Без них live generation недоступна с явной ошибкой конфигурации.
Запуск приложения и чтение библиотеки при этом работают.

Для embeddings дополнительно фиксируются модель, dimensions, цена, privacy и
совместимость индекса. Смена модели/dimensions создаёт новую версию индекса,
не смешивает несовместимые embeddings и не переписывает историю молча.

Для WEB_SEARCH_ENABLED/WEB_FETCH_ENABLED требуются SEARCH_API_KEY при выбранном
провайдере, SEARCH_PROVIDER, непустой SOURCE_DOMAIN_ALLOWLIST, лимиты и права.
Оба флага false по умолчанию; поиск и fetch можно включать отдельно.
Ключи поиска и API не передаются браузеру или модели.

Бюджеты включают исправления и проверки, а не только первый ответ.
Нужны учёт расходов по всем дням/месяцам UTC и отдельный учёт запросов,
начатых до смены периода.

## Режимы регистрации

closed: локальные/служебные аккаунты, публичная регистрация закрыта.
invite: регистрация по одноразовому коду без привязки к имени или email.
open: открытая регистрация с лимитами.

Для каждого созданного аккаунта welcome grant одинаков.
Нельзя использовать invite как основание не начислять обещанные кредиты.

## Включение продаж

Требуются provider credentials, seller details, pack catalog,
currency, webhook secret, approved payment terms, refund policy.
Без них TOPUP_MODE=payments должен проваливать deployment check.

## Версионирование

CreditPolicy version и PublicationPolicy version записываются при создании
job/submission. Изменения не переоценивают уже завершённые действия.
Применение нового порога к активным кандидатам — отдельная явная операция.

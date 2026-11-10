# Philosophy Reader

Интеллектуальный журнал: философия на простом английском с переводом
предложений по нажатию, личная ИИ-мастерская и кредитный учёт.

Django 5.2 LTS · Python 3.12 · PostgreSQL 16 + pgvector · серверные templates.

Полная спецификация: `docs/spec/` (версия 1.2, пакет `philosophy-reader-spec-v1.1`).

## Состав первого захода (этапы 1–4)

- Основа: custom User без email, настройки dev/test/prod, lock, health checks.
- Читательский продукт: главная, библиотека, читалка en/ru, подборки, мыслители.
- Аккаунт: регистрация (invite), welcome-грант 30 кредитов, кабинет,
  избранное, слова, баланс, безопасность.
- Кредитная очередь: ledger, резервы, AIJob, идемпотентность, FakeProvider
  (только dev/test), `run_ai_worker`, `run_maintenance`.

## Этап 5 — источники и RAG (выполнено)

- Импорт TXT/MD/PDF: `import_source --path --login [--title]`. Извлечение
  текста без выполнения содержимого (pypdf), нормализация, дедуп по hash —
  повторная загрузка не создаёт вторую версию. PDF без текстового слоя
  и «битые» PDF отклоняются с кодом причины.
- Chunking 300–600 слов по границам абзацев/предложений (предложение
  не разрывается ради фиксированного размера), небольшой overlap.
- FTS: генерируемая колонка `search_vector` (english) на `sources_sourcechunk`
  + GIN-индекс (миграция 0003, Django GeneratedField).
- Embeddings через OpenRouter (`openai/text-embedding-3-small`, 1536):
  `index_source --document <uuid> [--reindex]`. Батчи по 16, сеть — только
  вне транзакций БД; проверка размерности вектора перед записью.
- Гибридный поиск: `search_sources --login --query [--document --top]`.
  FTS + cosine по pgvector внутри scope (свои документы + редакционный
  корпус), детерминированное слияние, дедуп, top-N (8 по умолчанию).
  Чужие приватные документы недоступны — покрыто тестом изоляции scope.
- DocumentFetcher с SSRF-защитой: только http/https из allowlist, запрет
  loopback/private/link-local/metadata, повторная проверка DNS на каждом
  redirect, лимиты redirects/времени/размера/типа. Выключен по умолчанию
  (`WEB_FETCH_ENABLED=false`); включается владельцем вместе с
  `SOURCE_DOMAIN_ALLOWLIST`.
- Тесты: `tests/test_sources.py` (15), `tests/test_fetcher.py` (7) —
  извлечение, chunking, дедуп, изоляция scope, гибрид, SSRF.

Книга «Дневники Витгенштейна 1914–1916» (PDF, ~156 КБ) импортирована и
проиндексирована (42 фрагмента), гибридный smoke-поиск работает (RU и EN
запросы, обе ноги поиска).

## Этап 6 — живой OpenRouter (выполнено)

- `LIVE_AI_ENABLED=true`, `AI_PROVIDER=openrouter`, модели generation/review:
  `anthropic/claude-sonnet-5.5` ($2/$10 за 1M токенов). Budget-guard:
  `OWNER_DAILY_BUDGET_USD=1`, `OWNER_MONTHLY_BUDGET_USD=30` — проверка
  до сетевого вызова, запись в `credits_budgetbucket` после ответа.
- `OpenRouterProvider.generate` (apps/ai/openrouter.py): Chat Completions,
  `response_format json_schema` (strict), промпты из спецификации, учёт
  usage/cost в `ProviderAttempt`, списание в `BudgetReservation` (одна запись
  на job, day+month бакеты). `AI_MAX_PROVIDER_CALLS_PER_JOB=4`.
- Smoke-тест живой генерации через очередь: job → воркер → Sonnet 5.5 →
  ревизия сохранена, кредиты списаны (10), usage/cost зафиксированы.
  Embeddings (этап 5) уже идут через живой API (~$0.002 за всю книгу).

## Этап 7 — мастерская правок (выполнено)

- Правки предложения (`edit_sentence`, 1 кредит) и части (`edit_part`,
  3 кредита): цель задаёт сервер (`part` или `part:sentence`), промпт
  editing-v2 из спецификации. Модель возвращает только замены; новый полный
  снимок собирает сервер: неизменённые предложения и переводы копируются
  дословно, заголовки и glossary sentence-правкой не трогаются, part-правка
  меняет контент только одной части. Выход за границы ревизии отклоняется
  (`EditScopeError`).
- Review/repair-конвейер (`run_review_cycle`): review → (один repair →
  свежий review), максимум 4 вызова провайдера на job. Провал финального
  review → `no_charge_failure` с сохранённым бесплатным объяснением;
  `needs_broader_edit`/`needs_clarification`/`refused` — доставленные
  no-charge исходы с сохранённым сообщением, без ревизии и списания.
- Head автоматически ставится только у первой успешной серии (generate);
  правки создают ревизию без смены head — head двигается только через
  «Применить» (`POST /projects/{id}/head/`). Повторное применение — no-op.
- Сравнение ревизий: `GET /workshop/{project}/revisions/{id}/diff/`
  (серверный diff против head, только владелец).
- Payload ревизии иммутабелен на уровне модели: изменение сохранённого
  снимка вызывает `ValueError` — старый QualityReport не может подтвердить
  новый контент.

## Этап 8 — редакционная публикация и жалобы (выполнено)

- `publish_revision` (apps/library/services.py): гейты — свежий
  QualityReport для текущего content_hash, verified KnowledgeCard,
  `factual_state=supported`, нет blocking issues, права источников
  (`rights_state=allowed`; private+unknown блокирует). Идемпотентно;
  `AuditEvent(action="editorial_publish")` при создании. `retract_publication`
  — отзыв с аудитом.
- Admin-действие «Редакционная публикация выбранной ревизии» в admin
  ревизий с показом причин отказа; PublicationAdmin с аудитом.
- Жалобы: `POST /reports/<slug>/` (категории factual_error/too_complex/
  bad_translation/inappropriate/rights/other), лимит 3/сутки на
  пользователя, одиночная жалоба не скрывает материал.

## Этап 9 — экспорт и удаление аккаунта (выполнено)

- `GET /account/export/` — JSON-выгрузка всех данных пользователя (аккаунт,
  кредиты и транзакции, документы с чанками, проекты с ревизиями/сообщениями/
  задачами, избранное, слова) через `export_user_data`.
- `purge_user_data`: в одной транзакции отменяет queued-задачи, освобождает
  резервы без списания, удаляет RetrievalItem/ProviderAttempt, отвязывает
  AIJob (PROTECT), удаляет проекты/ревизии/документы/chunks/embeddings/
  избранное/слова/сообщения, деактивирует аккаунт (login → `deleted-<hex>`,
  public_alias «Автор удалён»), пишет AuditEvent. Финансовый ledger
  (Wallet/CreditTransaction) сохраняется — денежная история не стирается.
- Management-команды: `export_user_data --login [--output]`,
  `purge_user_data --login --reason` (отказ для активного staff).
- HTTP-удаление аккаунта (`POST /account/delete/`) выполняет purge немедленно.

## Требования

- Ubuntu 24.04, Python 3.12, PostgreSQL 16 с расширением `vector`.
- Отдельная БД и роль: `philosophy_reader`.

## Установка (выполнено на этом сервере)

```bash
sudo apt-get install -y postgresql-16 postgresql-16-pgvector
sudo -u postgres psql -c "CREATE ROLE philosophy_reader LOGIN PASSWORD '...';"
sudo -u postgres psql -c "CREATE DATABASE philosophy_reader OWNER philosophy_reader;"
sudo -u postgres psql -d philosophy_reader -c "CREATE EXTENSION vector;"
```

Сервисный пользователь `prdr` (без sudo и docker), проект в
`/opt/philosophy-reader`, venv в `/opt/philosophy-reader/venv`.

## Локальный запуск (dev)

```bash
cd /opt/philosophy-reader
cp .env.example .env   # заполнить DB_PASSWORD и DJANGO_SECRET_KEY
venv/bin/python manage.py migrate
venv/bin/python manage.py seed_demo
venv/bin/python manage.py runserver 127.0.0.1:8000
```

## Production-подобный запуск (loopback, Hermes не затрагивается)

Секреты БД/Django — в `/etc/philosophy-reader/service.env` (root:prdr, 0640),
в `.env` проекта только `OPENROUTER_API_KEY`.

```bash
sudo systemctl enable --now philosophy-reader-web      # Gunicorn 127.0.0.1:8801
sudo systemctl enable --now philosophy-reader-worker   # один AI-worker
sudo systemctl enable --now philosophy-reader-maintenance.timer  # каждые 15 мин
curl -s http://127.0.0.1:8801/health/
```

Юнит-файлы лежат в `deploy/`. Web работает с `MemoryHigh=400M` (1 worker ×
4 threads), worker — с `MemoryHigh=300M`; лимиты подобраны под VPS 2 GB,
где вместе с PostgreSQL и Docker (Hermes) запас памяти небольшой. Статика
сейчас отдаётся Gunicorn'ом через `config.wsgi_static` — временно, до
настройки nginx; тогда `wsgi_static` удаляется, а `/static/` уходит прокси.

## Проверки

```bash
venv/bin/python manage.py test --settings=config.settings.test
venv/bin/python manage.py check
```

## Management commands

| Команда | Назначение |
|---|---|
| `migrate` | Миграции |
| `createsuperuser` | Администратор (логин/пароль, без email) |
| `seed_demo` | Реальный демо-материал en/ru (Сократ/Платон) |
| `run_ai_worker --once` | Обработать одну задачу из очереди |
| `run_maintenance` | Зависшие задачи и резервы |
| `issue_credits` | Ручное пополнение: `--login --amount --operation-key --reason` (идемпотентно, только при `TOPUP_MODE=manual_test`) |
| `import_source` | Импорт TXT/MD/PDF: `--path --login [--title]`, дедуп по hash |
| `index_source` | Chunking + embeddings: `--document <uuid> [--reindex]` |
| `search_sources` | Гибридный FTS+vector поиск: `--login --query [--document --top]` |
| `export_user_data` | JSON-выгрузка данных пользователя: `--login [--output]` |
| `purge_user_data` | Удаление аккаунта и приватных данных: `--login --reason` (ledger сохраняется) |

## Известные ограничения (после этапа 9)

- Живой генератор — Claude Sonnet 5.5 через OpenRouter; FakeProvider остаётся
  для dev/test. Review-модель та же (Haiku 5.5 — кандидат на будущее, в 20 раз
  дешевле).
- Веб-поиск и fetch выключены по умолчанию; SearchAdapter (внешний
  поисковый API) ещё не подключён — DocumentFetcher готов и покрыт тестами.
- FTS сконфигурирован на 'english'; для русских запросов работает векторная
  нога и морфология PostgreSQL отсутствует (приемлемо для en-корпуса MVP).
- Editorial-публикация в seed_demo создаётся напрямую (сидирование, мимо
  гейтов publish_revision); retrieve-сценарий simplify_source в edit-потоке
  не покрыт end-to-end.
- nginx/HTTPS/публичный доступ не настроены (loopback only) — отдельный
  согласованный шаг после правового и платёжного контура.

Подробный журнал: `docs/IMPLEMENTATION_LOG.md`.

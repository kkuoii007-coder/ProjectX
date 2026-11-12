# Журнал реализации

Дата: 9 ноября 2026. Среда: Ubuntu (VPS 2 CPU / 1.9 GB RAM), Python 3.12.3,
Django 5.2.19, PostgreSQL 16 + pgvector, пользователь `prdr`, проект
`/opt/philosophy-reader`, БД `philosophy_reader`.

## Этап 1 — основа (выполнено, восстановлено)

Было сделано до этого захода (Kimi k3): структура `config/`, `apps/`
(accounts, credits, library, sources, workshop, ai, operations), custom User
без email, модели кредитов и задач, seed_demo, settings dev/test/prod,
lock-файл.

Восстановлено в этом заходе:

- Миграции отсутствовали — сгенерированы и применены все (`migrate` OK).
- Циклическая зависимость library↔workshop устранена разнесением на 0001/0002.
- `sources.0002_enable_vector`: создание расширения `vector` миграцией — тестовая
  БД теперь разворачивается без ручных шагов.
- Баг `grant_welcome_credits` (`select_for_update` вне транзакции) исправлен:
  вызов безопасен и вне atomic-блока.
- Баг `create_job`: `Wallet` создаётся лениво через `get_or_create`.
- `seed_demo` был неидемпотентен (дубли ревизий и отчётов) — исправлен: поиск
  ревизии по content_hash, отчёта по (revision, hash); проверено 3 запуска.
- Исправлены URL-namespace конфликты (`accounts` vs `account`) и
  LOGIN_REDIRECT_URL.

Проверки: `check`, `migrate`, `seed_demo` ×3, HTTP smoke (health/home/read),
`createsuperuser` без email, грант-идемпотентность, идемпотентность задач,
конфликт payload, insufficient credits, жизненный цикл job (queued → running →
succeeded, списание один раз, release при ошибке).

## Этап 2 — читательский продукт (проверен)

Главная, библиотека с поиском, читалка `/read/<slug>/` с переводом предложений
по клику/Enter/Space (готовые данные, без вызовов модели), мыслители, подборки,
пустые состояния. Проверено: гостевые страницы 200, перевод рендерится из
сохранённого payload, чтение не расходует кредиты.

## Этап 3 — аккаунт (завершён в этом заходе)

- Добавлены лимиты входа/регистрации на псевдонимизированном сетевом признаке
  (HMAC с дневной ротацией, TTL ≤ 24 ч): 10/аккаунт/15 мин, 30/сеть/15 мин,
  3 регистрации/сеть/сутки, 50 глобальных/сутки.
- `RegistrationEvent` и `LoginAttempt` записываются до/вместо дорогой проверки
  пароля.
- logout-all и смена пароля отзывают сеансы: `auth_epoch` включён в
  session auth hash (`User.get_session_auth_hash`).
- Экспорт данных `GET /account/export/` (JSON attachment) — только свои данные.
- Инвайт-режим: обязательный код, одноразовое использование, блокировка
  повторного применения.
- Административное пополнение: `issue_credits --login --amount --operation-key
  --reason`, идемпотентно по operation_key, kind=topup.
- Welcome grant: ровно один (Unique(user, campaign)), начисляется в транзакции
  регистрации, 30 кредитов.

## Этап 4 — кредитный фундамент (проверен и покрыт тестами)

Wallet constraints (total≥0, reserved≥0, reserved≤total), ledger с уникальными
operation_key, резерв/списание/возврат ровно один раз, поздний результат не
списывает завершённую неудачей задачу, конфликт несовпадающего payload,
недостаток средств не оставляет резервов, lease и восстановление зависших задач.
Наборы: `tests/test_credits` (6), `tests/test_jobs` (11), `tests/test_accounts`
(8) — все проходят в изолированной тестовой БД с pgvector (`--parallel=1`).

## Этап 5 — источники и RAG (выполнен 10 ноября 2026)

- Импорт/извлечение: `apps/sources/services.py` — TXT/MD (utf-8), PDF через
  pypdf (без выполнения содержимого), нормализация (де-дефисация, абзацы,
  маркеры страниц), chunking 300–600 слов по абзацам/предложениям с overlap.
  Дедуп: повтор документа по (owner, content_hash) не создаёт новую версию.
- FTS: миграция 0003 — `search_vector` как PostgreSQL GENERATED ALWAYS
  STORED (`to_tsvector('english', text)`) + GIN-индекс. В модели — Django
  `GeneratedField` (INSERT/UPDATE не пишут колонку автоматически).
- Embeddings: `apps/sources/embeddings.py` — OpenRouter `POST /api/v1/embeddings`,
  модель `openai/text-embedding-3-small` (1536), батчи 16, проверка размерности
  и соответствия числа векторов; сеть только вне транзакций; индексация
  последовательная (память ограничена).
- Retrieval: `apps/sources/retrieval.py` — гибрид FTS (`SearchRank`, websearch)
  + pgvector cosine внутри scope (owner private ∪ editorial), нормировка ног
  по максимуму, среднее, дедуп по chunk, top-N=8. При недоступности embeddings
  FTS-нога продолжает работать; фальшивых результатов нет.
- Команды: `import_source`, `index_source`, `search_sources`.
- DocumentFetcher: `apps/sources/fetcher.py` — allowlist доменов, запрет
  loopback/private/link-local/metadata, DNS перепроверяется на каждом redirect
  (анти-rebinding), лимиты redirect/время/размер/content-type. Выключен
  по умолчанию (`WEB_FETCH_ENABLED=false`).
- Тесты: 15 (sources) + 7 (fetcher) новых; полный набор 44 — все проходят
  в изолированной тестовой БД с pgvector.
- На реальных данных: «Дневники Витгенштейна 1914–1916» (PDF, 156 КБ,
  русский текст) импортирован (42 фрагмента), embeddings записаны, гибридный
  smoke-поиск проверен на RU/EN запросах; обе ноги возвращают релевантное.
- Решение в рамках этапа: FTS-конфиг 'english' (корпус en); для RU-запросов
  semantic-нога компенсирует морфологию. Ограничение зафиксировано в README.

## Этап 6 — живой OpenRouter (выполнен 10 ноября 2026)

- Решение владельца: `OWNER_DAILY_BUDGET_USD=1`, `OWNER_MONTHLY_BUDGET_USD=30`,
  модели generation/review — `anthropic/claude-sonnet-5.5` (выбор по качеству
  перевода философского текста: лучший scholarly register в независимом
  бенчмарке академического перевода при цене $2/$10 за 1M токенов).
- `apps/ai/openrouter.py`: Chat Completions с `response_format json_schema`
  (strict), промпты из `docs/spec/prompts/`, потолок цены по конфигурации,
  учёт usage/cost в `ProviderAttempt`, бюджет-гвард до сетевого вызова
  (дневной/месячный бакеты), списание после ответа — одна `BudgetReservation`
  на job, capture в оба бакета.
- `get_provider()` в services.py: `LIVE_AI_ENABLED=true` + `AI_PROVIDER=openrouter`
  → живой провайдер; иначе FakeProvider. JobError при неполной конфигурации.
- Smoke-тест живой генерации: job → воркер → Sonnet 5.5 → ревизия сохранена
  (payload с title/вопросами/частями/предложениями en+ru), 10 кредитов
  списаны, usage/cost записаны в `ProviderAttempt`.
- В процессе найдено и исправлено: право БД на `ai_providerattempt` (миграция
  под root отняла права у роли приложения — GRANT восстановил), дублирование
  `BudgetReservation` (OneToOne на job), отступы в `fetcher.py` и
  `services.py`.
- Тесты: 44/44 (FakeProvider остаётся в dev/test; живой путь — только в prod).

## Этап 7 — мастерская правок (выполнен 10 ноября 2026)

- `apps/ai/openrouter.py`: схемы `EDIT_SENTENCE_SCHEMA`/`EDIT_PART_SCHEMA`
  (strict json_schema: 1–2 предложения замены для sentence; полный текст
  части для part), промпт editing-v2 из `docs/spec/prompts/fragment-editing.md`,
  `REVIEW_SCHEMA` + repair-v2. `AI_MAX_PROVIDER_CALLS_PER_JOB=4`.
- Сервер владеет областью правки: цель (`part` или `part:sentence`) и текст
  фрагмента передаются провайдеру из сохранённого снимка
  (`_build_edit_context`), пользовательский ввод не задаёт scope. Новый
  полный снимок собирает `apply_edit`: неизменённые предложения/переводы
  копируются дословно, заголовки и glossary sentence-правкой не трогаются,
  part-правка меняет контент только одной части. Выход за границы →
  `EditScopeError` (задача failed, резерв освобождён).
- `run_review_cycle` в services (общий для Fake и OpenRouter): review →
  (repair → свежий review), максимум 4 вызова. Исходы: pass → delivered;
  fail → `no_charge_failure` (только с сохранённым бесплатным сообщением
  kind="review"); needs_broader_edit/needs_clarification/refused → Message
  kind="explanation", без ревизии и списания.
- Head: автоматически ставится только у первой успешной серии (generate);
  правки создают ревизию без смены head. `POST /projects/{id}/head/`
  применяет ревизию (повтор — no-op, не откат). Diff ревизии против head:
  `GET /workshop/{id}/revisions/{rev}/diff/`.
- `Revision.save` enforcing immutability: изменение payload сохранённой
  ревизии → `ValueError` (старый отчёт не подтверждает новый контент).
- Формы в мастерской: правка части/предложения inline, таблица версий
  с «Сравнить»/«Применить», обсуждение (ask).

## Этап 8 — редакционная публикация и жалобы (выполнен 10 ноября 2026)

- `publish_revision` (apps/library/services.py): гейты — QualityReport для
  текущего content_hash, verified KnowledgeCard, `factual_state=supported`,
  нет blocking issues, права источников (allowed; private+unknown блокирует).
  Идемпотентно; AuditEvent(action="editorial_publish") при создании.
  `retract_publication` — отзыв с аудитом. Admin-действие публикации в
  admin ревизий с показом причин отказа (`PublicationBlocked`).
- Жалобы `POST /reports/<slug>/`: категории factual_error/too_complex/
  bad_translation/inappropriate/rights/other, лимит 3/сутки (429), 401
  без входа, одиночная жалоба не скрывает материал (spec).

## Этап 9 — экспорт и удаление аккаунта (выполнен 10 ноября 2026)

- `export_user_data` (apps/accounts/export_service.py): аккаунт, кредиты и
  транзакции, документы с текстом и чанками, проекты (ревизии, сообщения,
  jobs), избранное, слова; только свои данные, даты в isoformat.
  Management-команда `export_user_data --login [--output]` и HTTP
  `GET /account/export/`.
- `purge_user_data` (apps/accounts/purge_service.py): в одной транзакции —
  отмена queued jobs, освобождение активных резервов без списания
  (FAILED помечается error_code="account_deleted"), удаление
  RetrievalItem/ProviderAttempt, отвязка AIJob (PROTECT), удаление проектов/
  ревизий/EvidenceLink/документов/chunks/embeddings/избранного/слов/сообщений,
  LoginAttempt по логину; аккаунт деактивируется: login → `deleted-<hex>`,
  public_alias «Автор удалён», unusable password, AuditEvent(action=
  "account_purge"). Wallet и CreditTransaction сохраняются — денежный ledger
  не стирается. Команда `purge_user_data --login --reason` отказывает для
  активного staff. HTTP `POST /account/delete/` вызывает purge немедленно.

## Проверки после этапов 7–9

- Тесты: 78/78 (`manage.py test --settings=config.settings.test`):
  границы правки и неизменённые фрагменты, review/repair исходы,
  применение head, гейты публикации, жалобы, export/purge.
- Живой smoke на OpenRouter (Sonnet 5.5, пользователь smoketest):
  generate → succeeded/delivered, ревизия + head, −10 кредитов;
  edit_sentence [1:1] → заменена одна цель, соседние нетронуты, −1 кредит,
  head не тронут; edit_part (часть 3) → пример добавлен, другие части
  нетронуты, −3 кредита; diff показывает только изменённое предложение;
  apply_head → head=10, повтор — no-op.
- В процессе найдено и исправлено:
  - `BudgetReservation.job` был OneToOne при 4 платных вызовах на job —
    второй вызов (_record_cost) падал на unique constraint; миграция 0002
    переводит на ForeignKey (spec: «job/attempt»).
  - HTTP 500 на `/projects/new/` при отсутствии кошелька/кредитов: рендер
    402-страницы внутри прерванной atomic-блока ронял контекст-процессор
    `account_summary`; теперь 402 рендерится вне транзакции, проект не
    создаётся; добавлены 409 на JobConflict/QueueFull и регрессионный тест.
  - `edit_fragment` отклонял bare part index для edit_part — валидация
    разделена по видам цели.
  - Модель без контекста фрагмента отвечала needs_clarification («контекст
    пустой») — добавлен `_build_edit_context`.
- Права каталогов миграций после миграций под root проверены и исправлены
  (`chown prdr:prdr`, `u+rwX,go-rwx`) — после любого `makemigrations`/`migrate`
  под root повторять.

## Следующий этап

git init + первый коммит (проверка .gitignore на секреты); решение по nginx
и публичному доступу — за владельцем (домен/HTTPS/правовой контур).

## Эксплуатация (настроено в этом заходе)

- `/etc/philosophy-reader/service.env` (0640 root:prdr): DB_NAME/DB_USER/
  DB_PASSWORD/DJANGO_SECRET_KEY/DJANGO_SETTINGS_MODULE=config.settings.prod;
  с этапа 6 также OWNER_DAILY_BUDGET_USD/OWNER_MONTHLY_BUDGET_USD,
  LIVE_AI_ENABLED, AI_PROVIDER, AI_GENERATION_MODEL, AI_REVIEW_MODEL
  (бэкап исходного файла — `service.env.bak-20261110`).
- `/opt/philosophy-reader/.env` (0600 prdr): только OPENROUTER_API_KEY.
- systemd: `philosophy-reader-web` (Gunicorn 1 worker × 4 threads,
  127.0.0.1:8801, MemoryHigh 400M), `philosophy-reader-worker` (один
  последовательный AI-worker, MemoryHigh 300M), `philosophy-reader-maintenance`
  (oneshot) + таймер каждые 15 минут. Юнит-файлы хранятся в `deploy/`.
- Статика: `collectstatic` выполнен; для loopback-запуска без reverse proxy
  Gunicorn использует `config.wsgi_static:handler` (StaticFilesHandler).
  После согласования nginx обрабатывать статику будет прокси, wsgi_static
  удаляется.
- Проверено: `/health/` → `{"web":"ok","database":"ok","vector":"ok"}`,
  home/login/register/static 200, worker active, maintenance завершается
  успешно, Hermes не затронут (контейнер healthy, сеть 172.22.0.0/16 не
  пересекается), наружу открыт только SSH.

## Ограничения и состояние секретов

- Регистрация закрыта (`REGISTRATION_MODE=invite` по умолчанию), продажи
  выключены (`TOPUP_MODE=manual_test`). Живой ИИ включён с этапа 6
  (`LIVE_AI_ENABLED=true`, `AI_PROVIDER=openrouter`, дневной бюджет $1,
  месячный $30); FakeProvider остаётся только для dev/test.
- В `/root/philosophy-reader-spec-v1.1/.env` хранился незашифрованный
  OPENROUTER_API_KEY рядом со спецификацией; ключ перенесён в
  `/opt/philosophy-reader/.env` (0600), права на исходный файл ужесточены до
  0600. Рекомендация владельцу: считать ключ скомпрометированным при
  поступлении в открытый репозиторий пакета и заменить при первой настройке
  живого ИИ; из пакета спецификации его убрать.
- Открытый порт 8801 наружу не привязан; HTTPS/публичный доступ требуют
  отдельного решения владельца (домен, правовой контур).

## Frontend-редизайн (выполнен)

Направление «Глосса на полях»; стек: шаблоны + ручной CSS на токенах +
vanilla JS + шрифты Literata/Golos Text (OFL, self-hosted). Миграций и новых
API нет; логика views не менялась — только контекст и разметка шаблонов.

### Этап 1 (коммит f7d48aa): главная, читалка, base

- `base.html`: header с брендом-маркой ¶, мобильное меню на кнопке, лента
  notices, footer. Главная: hero со specimen-карточкой (демо-предложение с
  глоссом), featured, списки, ночная лента мастерской.
- Читалка: перевод — заметка на полях (`span.s[role=button]` +
  `span.gloss[role=note]`; справа на ≥82rem, под предложением на узких),
  reading-line, rail частей, термины, жалобы. No-JS: перевод виден, меню
  раскрыто.

### Этап 2 (коммит dd6bb72): все остальные страницы

- Библиотека: каталог (page-head + поиск + карточки), подборки, мыслители.
- Кабинет: dashboard (account-nav, пустые состояния с CTA, история задач),
  кредиты (balance-figure, секции), избранное, слова, безопасность, вход,
  регистрация, регистрация закрыта.
- Мастерская: create.html и project.html переведены на тёмную ленту
  `.workshop-page` (общий стиль с главной); страница проекта — переход с
  legacy `.sentence/.translation` на каноничную разметку `span.s`/`span.gloss`
  с `aria-controls`. Таблица версий и обсуждение — на бумажной части ниже
  ленты.
- `reader.js`: удалён `initLegacySentences` — legacy-разметки больше нет.
- `main.css`: новые секции (`.page-head`, `.search-form`, `.publication-card`,
  `.account-nav`, `.account-section`, `.balance-figure`, `.form-page`,
  `.workshop-page`, `.discourse`, `.edit-sentence/.edit-part-form`), сняты
  дубли правил. Критичный контракт сохранён: project.html безопасно рендерится
  при контексте только `{project, error}` (ошибки ask/edit_fragment) — секции
  версий и обсуждения обёрнуты в `{% empty %}`/{% if %}-защиты.

Проверки: 102 теста OK (в `tests/test_frontend.py` добавлен
`RedesignedPagesContractTests` — 7 тестов контракта новых страниц: page-head/
search на библиотеке, кабинет, кредиты, логин, глоcc-разметка проекта,
рендер ошибок без контекста). Production-деплой: коммит перенесён fast-forward
в боевую main, collectstatic (2 файла), рестарт web+worker, smoke 200 на
главной/библиотеке/читалке/справке/логине, кабинет/кредиты/безопасность и
страница проекта отдают новую разметку, POST ask валидный → 302, пустой
инструкция → 400 с корректным рендером ошибки.
Не проверено: визуальная оценка «глазами» (скриншоты сняты, не просмотрены),
реальные браузеры Safari/Firefox, скринридеры.

## Production hardening (выполнен 12 ноября 2026)

Цель — закрыть технические дефекты прод-готовности до решения владельца
о публичном доступе (nginx/HTTPS/домен остаются отложенным шагом).

- `config/settings/prod.py`: недействующий в Django 5.2
  `STATICFILES_STORAGE = ManifestStaticFilesStorage` заменён на dict
  `STORAGES` (default → FileSystemStorage, staticfiles →
  ManifestStaticFilesStorage). Добавлен явный `LOGGING` (root INFO,
  `django.request` ERROR → stderr): при DEBUG=False ошибки приложения
  теперь видны в journalctl юнитов web/worker.
- `config/wsgi_static.py`: stock `StaticFilesHandler` резолвит URL через
  finders (исходные директории) и не видит post-processed hashed-файлов в
  `STATIC_ROOT`; хендлер заменён на отдачу напрямую из `STATIC_ROOT`
  (`django.views.static.serve` с `document_root=STATIC_ROOT`). Манифест-URL
  (`css/main.<hash>.css`, шрифты, `reader.js`) отдаются корректно.
- `collectstatic --clear` с prod-настройками: 137 файлов post-processed,
  создан `staticfiles/staticfiles.json`; страницы ссылаются на hashed-URL.
- Ежедневный бэкап БД: `deploy/backup-philosophy-reader.sh` (pg_dump,
  custom-формат, 0700) → `/var/backups/philosophy-reader/`, retention 7
  дней, запуск от `postgres` (peer auth) через
  `/etc/cron.d/philosophy-reader-backup` в 03:35 UTC, лог
  `/var/log/philosophy-reader-backup.log`. Первый дамп снят и проверен
  (`pg_restore --list`: 141 объектов TABLE DATA/SEQUENCE, 568 КБ).
- Проверки: 102 теста OK; `check` и `check --deploy` (prod) — только 3
  ожидаемых HTTPS-warning (HSTS subdomains/preload, SSL_REDIRECT), они
  включаются при появлении публичного домена; рестарт web+worker, health
  `{"web":"ok","database":"ok","vector":"ok"}`; smoke 200: главная,
  библиотека, читалка, справка о кредитах, логин, admin; все hashed-статик
  ресурсы со страниц 200; ошибок в journalctl web/worker нет.
- Изменено серверное окружение вне репозитория:
  `/usr/local/sbin/backup-philosophy-reader` (копия скрипта),
  `/etc/cron.d/philosophy-reader-backup`, каталог
  `/var/backups/philosophy-reader` (postgres:postgres 0700).

## Публичный доступ и защита сервера (выполнены 12 ноября 2026)

Решение владельца: открыть сайт по IP (домена нет), «не оставив шансов».

### Аудит-предусловия

- Репозиторий `github.com/kkuoii007-coder/ProjectX` публичный (HTTP 200),
  но полная git-история обоих клонов не содержит `sk-or-` токенов;
  `.env` никогда не коммитился (gitignore работает). Утечка ключа была
  не в git, а в файле пакета спецификации на диске — ротация остаётся
  на владельце.
- До работ: парольный SSH был открыт через cloud-init override
  (`PasswordAuthentication yes`), фаервола не было (INPUT ACCEPT),
  nginx 1.24 установлен, но ничего не раздавал.

### Защита ОС

- SSH: удалён `/etc/ssh/sshd_config.d/50-cloud-init.conf`
  (PasswordAuthentication no эффективно), `PermitRootLogin prohibit-password`.
  Стабильность key-only входа подтверждена (сессии не рвутся, повторные
  подключения проходят; probes по ssh-rsa отклоняются).
- nftables (`deploy/nftables.conf` → `/etc/nftables.conf`, сервис включён):
  input policy drop, разрешены loopback, established/related, 22/80/443,
  ICMP (v4) и весь icmpv6 (NDP нужен для v6-связности), лог дропов
  5/мин. Docker-сети Hermes (172.17.0.0/16, 172.22.0.0/16) не фильтруются
  (контейнер hermes-gateway не публикует портов). Переживает restart
  и загрузку (enabled).
- fail2ban (`deploy/fail2ban-jail.local` → `/etc/fail2ban/jail.local`,
  banaction nftables): sshd aggressive (3 попытки, ban 1h, эскалация ×2
  до 1w), nginx-http-auth, nginx-botsearch, nginx-limit-req. Уже
  забанил первых сканеров SSH (121.224.5.228, 222.210.35.197 и др.).
- Проверено: наружу слушают только 22/80/443 (v4+v6); Postgres/Gunicorn/
  docker-порты — loopback/private.

### TLS и nginx

- Self-signed сертификат на 10 лет (rsa 3072, SHA-256) с SAN
  IP:185.112.100.238, IP:2a0c:5d00:2002::7e23:46b7, DNS:shults.ru
  (`deploy/openssl-san.cnf`); ключ/серт в `/etc/philosophy-reader/tls/`
  (0600/0644).
- nginx (`deploy/nginx-philosophy-reader.conf` → sites-enabled):
  80 → 301 → https; 443 ssl http2 (TLSv1.2/1.3, session cache,
  tickets off, server_tokens off, OCSP stapling off — нет responder'а
  для self-signed); proxy к 127.0.0.1:8801 с X-Forwarded-Proto;
  статика из STATIC_ROOT с immutable; limit_req 6 r/m login/register/
  admin-login и 30 r/m общий (429 при превышении); security-заголовки;
  client_max_body_size 6m; ACME-заготовка на 80 для будущего домена.
- Права: `chmod o+x /opt/philosophy-reader` — www-data получает доступ
  на чтение staticfiles через путь (каталог проекта оставлен 0750 prdr).

### Приложение

- `config/settings/prod.py`: `SECURE_PROXY_SSL_HEADER`, HSTS
  includeSubDomains+preload, `CSRF_TRUSTED_ORIGINS` (оба IP),
  `SECURE_SSL_REDIRECT` через `DJANGO_SECURE_SSL_REDIRECT=true`
  (в service.env; бэкап `service.env.bak-20261112`).
- service.env: добавлены `DJANGO_ALLOWED_HOSTS` (оба IP + loopback) и
  `DJANGO_CSRF_TRUSTED_ORIGINS`.
- Юнит web переключён на `config.wsgi:application`; `config/wsgi_static.py`
  удалён (контракт из README «после nginx» выполнен).
- CSRF-поведение подтверждено: POST с корректным IP-origin и токеном →
  200 (форма с ошибкой входа), с чужим Origin → 403.

### Review-модель Haiku 5.5

- `AI_REVIEW_MODEL=anthropic/claude-haiku-5.5` в service.env
  ($0.10/$0.50 за 1M токенов — в 20× дешевле Sonnet 5.5 $2/$10);
  цена валидирована против `AI_STAGE_PRICE_CEILINGS["review"]` (3/15).
- Живой smoke через очередь: edit_sentence и ask jobs прошли полный
  конвейер (generate/edit → review → repair → re-review), попытки
  корректно регистрируются в ProviderAttempt c моделью haiku/sonnet,
  бюджет учитывается; бюджет-гард сработал штатно при исчерпании
  дневного лимита ($1) в ходе smoke-тестов ($0.94 израсходовано за
  17 вызовов).
- Наблюдение: Haiku строже к источникам — несколько заданий закрылись
  `no_charge_failure` с бесплатным объяснением (по спеке: неподтверждённое
  не выдаётся за полезный текст). Порог настраивается владельцем при
  необходимости (вернуть Sonnet или усилить контекст карточек).

### Off-site бэкапы

- Механика: `deploy/push-offsite-backup.sh` (в `/usr/local/sbin/`)
  шифрует свежий дамп AES-256-CBC/PBKDF2 600k итераций ключом
  `/etc/philosophy-reader/backup-encryption.key` (0600, создан),
  затем rclone copy в `philosophy-backup:philosophy-reader/backups`.
- Roundtrip шифрования проверен локально: расшифрованный дамп идентичен
  оригиналу (cmp), pg_restore видит 49 TABLE DATA.
- Cron `deploy/philosophy-reader-offsite.cron` (04:05 UTC) и
  пошаговая инструкция владельцу: `deploy/README-offsite-backup.md`.
  Активация — после того как владелец создаст remote (S3/B2/SFTP)
  через `rclone config --config /etc/philosophy-reader/rclone.conf`.

### Полная верификация

- 102 теста OK; `check` и `check --deploy` — 0 предупреждений (все
  HTTPS-пункты закрыты).
- Внешний smoke: https по v4 и v6 — 200 (главная/библиотека/читалка/
  логин/статика), http → 301 → https; rate limit: 8-й POST подряд на
  login получает 429; TLS 1.1 отклоняется, TLS 1.3 работает
  (TLS_AES_256_GCM_SHA384).
- Восстановление: дамп из каталога бэкапов развёрнут в тестовую БД
  `pr_restore_test` (3 пользователя, 5 ревизий, 42 chunks) и удалена.
- Сервисы после всех переключений: web/worker/maintenance/nftables/
  fail2ban/nginx — active; память в пределах лимитов.

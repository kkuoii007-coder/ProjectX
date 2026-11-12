# Off-site backups: как подключить удалённое хранилище

Состояние на сервере: дампы БД создаются ежедневно (03:35 UTC) в
`/var/backups/philosophy-reader/` (7 дней, 0700 postgres). Сценарий push
`/usr/local/sbin/push-offsite-backup` готов, но выключен, пока владелец
не подключит удалённое хранилище.

## Зачем это нужно

Дамп на том же диске, что и рабочая БД, не защищает от потери диска,
ransomware или утраты самой VPS. Off-site копия — единственная защита
в этих сценариях. Поэтому шаги ниже стоит выполнить.

## Шаги владельца (5 минут)

1. Заведите удалённое хранилище. Подходит любой из вариантов:
   - S3-совместимое облако (Backblaze B2 — самый дешёвый, Wasabi, Selectel,
     Yandex Object Storage, Hetzner Storage Box по SFTP — тоже ок);
   - обычный SFTP-аккаунт на другом сервере.
   Объём: дамп сейчас ~0.6 МБ сжатый; 7 копий + запас — план на годы.

2. Создайте ключ шифрования (один раз, храните также вне сервера):

   ```bash
   openssl rand -base64 48 | sudo tee /etc/philosophy-reader/backup-encryption.key
   sudo chmod 0600 /etc/philosophy-reader/backup-encryption.key
   ```

   БЕЗ ЭТОГО КЛЮЧА ЗАШИФРОВАННЫЙ ДАМП НЕВОЗМОЖНО ВОССТАНОВИТЬ.
   Сохраните копию ключа в менеджере паролей.

3. Настройте rclone в выделенный конфиг:

   ```bash
   sudo rclone config --config /etc/philosophy-reader/rclone.conf
   # имя remote: philosophy-backup  (используется в push-скрипте)
   sudo chmod 0600 /etc/philosophy-reader/rclone.conf
   ```

4. Проверьте push вручную:

   ```bash
   sudo /usr/local/sbin/push-offsite-backup
   sudo rclone ls --config /etc/philosophy-reader/rclone.conf philosophy-backup:philosophy-reader/backups
   ```

5. Включите cron-push (файл уже готов, просто активируйте):

   ```bash
   sudo install -m 0644 /opt/philosophy-reader/deploy/philosophy-reader-offsite.cron /etc/cron.d/
   ```

## Восстановление из off-site копии

```bash
# 1. скачать последний .enc
rclone copy --config /etc/philosophy-reader/rclone.conf \
  philosophy-backup:philosophy-reader/backups /tmp/restore/
# 2. расшифровать
openssl enc -d -aes-256-cbc -pbkdf2 -iter 600000 \
  -in /tmp/restore/db-YYYYMMDD-HHMMSS.dump.enc \
  -out /tmp/restore/db.dump -pass file:/etc/philosophy-reader/backup-encryption.key
# 3. восстановить
sudo -u postgres pg_restore -c -U philosophy_reader -d philosophy_reader /tmp/restore/db.dump
```

Потеря ключа = потеря бэкапов. Держите копию ключа вне сервера.

## Тестовое шифрование/расшифрование (без удалённого хранилища)

Проверить цикл уже можно локально: push-скрипт создаёт `.enc` рядом с дампом
и удаляет его после успешной отправки; для пробы без remote временно
прокомментируйте строки rclone в скрипте и убедитесь, что `openssl -d`
восстанавливает дамп (запись «восстановление из шифрованной копии»
в IMPLEMENTATION_LOG).

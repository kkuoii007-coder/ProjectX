"""Vocabulary profile of a text file on the CEFR-J word list.

Prints the share of words per CEFR level, the words above A1 and the
longest sentences. Counted are the sentences of the parts and the lines of
the forks (the question and the three answers). This is a check for the
editor before submitting a text; it does not touch the database. The exit
code is 1 when a submission gate would refuse the text.

The word list is the CEFR-J Vocabulary Profile (ver 1.5), see
content/wordlists/README.md for the source and terms of use. The level of
a word is the lowest among its parts of speech; proper nouns are not
counted as vocabulary.
"""

from django.core.management.base import BaseCommand, CommandError

from apps.library.editorial import EditorialError, load_cefr_list, profile_vocabulary, vocabulary_errors

from ._editorial import read_json


class Command(BaseCommand):
    help = "Print the CEFR vocabulary profile of a text JSON file (no database access)."

    def add_arguments(self, parser):
        parser.add_argument("path", help="content/texts/<slug>.json")
        parser.add_argument("--list", default="", help="Path to the CEFR-J CSV (default: content/wordlists)")

    def handle(self, *args, **options):
        data = read_json(options["path"])
        payload = data.get("payload") if isinstance(data, dict) else None
        if not isinstance(payload, dict):
            raise CommandError("Файл не содержит payload — это не файл текста")
        try:
            cefr_levels = load_cefr_list(options["list"] or None)
            profile = profile_vocabulary(payload, cefr_levels=cefr_levels)
        except EditorialError as exc:
            raise CommandError(str(exc)) from exc

        total = profile["total_words"]
        if not total:
            raise CommandError("В тексте нет английских слов")
        self.stdout.write(f"Всего английских слов в частях и развилках: {total} (из них в развилках: {profile['fork_words']})")
        for level, count in profile["levels"].items():
            self.stdout.write(f"  {level}: {count} ({profile['share'][level]:.1f} %)")
        self.stdout.write(f"Доля A1 только в частях: {profile['parts_a1_share']:.1f} %")
        self.stdout.write("Слова выше A1:")
        above = []
        for word in sorted(profile["above_a1"]):
            info = profile["above_a1"][word]
            mark = "в словарике" if info["in_glossary"] else "НЕТ в словарике"
            level = info["level"] or "нет в списке"
            above.append(f"{word} ({level}, x{info['count']}, {mark})")
        if above:
            for entry in above:
                self.stdout.write(f"  {entry}")
        else:
            self.stdout.write("  нет")
        self.stdout.write("Слова выше A2 без словарика (блокирует подачу):")
        uncovered = sorted(word for word, info in profile["above_a2"].items() if not info["in_glossary"])
        self.stdout.write("  " + (", ".join(uncovered) if uncovered else "нет"))
        self.stdout.write("Самые длинные предложения:")
        for words, text in profile["sentences"][:3]:
            self.stdout.write(f"  {words} слов: {text}")
        errors = vocabulary_errors(payload, cefr_levels=cefr_levels)
        if errors:
            self.stdout.write("Итог: подача будет отклонена")
            for error in errors:
                self.stdout.write(f"  {error}")
            raise SystemExit(1)
        self.stdout.write("Итог: словарная проверка пройдена")

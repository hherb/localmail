# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Horst Herb

"""Synthetic multilingual email corpus for Phase 1 acceptance.

Builds ~50 emails across de / en / es / no / ja using existing `_eml.py`
patterns (no .eml files on disk). Each message has a short subject and a
2-4 sentence body in the target language. The corpus is intentionally
small so that author-supplied ground-truth queries are tractable to
verify by eye; it's not a benchmark suite.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

_BASE = datetime(2025, 1, 1, tzinfo=timezone.utc)

# (lang, subject, body)
_SEED: list[tuple[str, str, str]] = [
    # German (10)
    ("de", "Konferenz Berlin", "Wir treffen uns nächste Woche zur Konferenz in Berlin. Bitte bringe das Programm mit."),
    ("de", "Mittagessen morgen", "Hast du Lust auf Mittagessen morgen um 12:30 im Café am Markt?"),
    ("de", "Reisekostenabrechnung", "Bitte reiche deine Reisekostenabrechnung bis Freitag ein, sonst verzögert sich die Auszahlung."),
    ("de", "Geburtstagsgeschenk Mama", "Was schenken wir Mama zum 70. Geburtstag? Hast du Ideen?"),
    ("de", "Urlaub Toskana", "Wir buchen unseren Urlaub in der Toskana für September. Drei Wochen."),
    ("de", "Arzttermin Verschoben", "Mein Arzttermin wurde auf Dienstag nächster Woche verschoben."),
    ("de", "Re: Rechnungen Q3", "Anbei die offenen Rechnungen für das dritte Quartal. Bitte zur Freigabe."),
    ("de", "Wohnungsbesichtigung", "Die Wohnungsbesichtigung in Hamburg-Eppendorf ist am Samstag um 14 Uhr."),
    ("de", "Heizung defekt", "Die Heizung ist seit gestern Abend ausgefallen. Hast du eine Empfehlung für einen Klempner?"),
    ("de", "Konferenzprogramm anbei", "Anbei das Programm für die Berliner Konferenz. Donnerstag Plenarsitzung im Saal Berlin."),
    # English (10)
    ("en", "Berlin conference next week", "Looking forward to the conference in Berlin next week. Please bring the agenda."),
    ("en", "Lunch tomorrow", "Want to grab lunch tomorrow at 12:30 at the corner café?"),
    ("en", "Q3 expense reports", "Please submit your Q3 expense reports by Friday to avoid payment delays."),
    ("en", "Mom's 70th birthday", "What should we get Mom for her 70th birthday? Any ideas?"),
    ("en", "Tuscany vacation booked", "We've booked the Tuscany vacation for September. Three weeks."),
    ("en", "Doctor appointment moved", "My doctor's appointment got moved to next Tuesday."),
    ("en", "Re: Q3 invoices", "Attached are the outstanding Q3 invoices for your approval."),
    ("en", "Apartment viewing Hamburg", "Apartment viewing in Hamburg-Eppendorf this Saturday at 2pm."),
    ("en", "Heating broken", "Heating's been out since last night. Any plumber recommendations?"),
    ("en", "Conference program attached", "Attached the program for the Berlin conference. Thursday plenary in Hall Berlin."),
    # Spanish (10)
    ("es", "Conferencia en Berlín", "Nos vemos la próxima semana en la conferencia de Berlín. Trae el programa por favor."),
    ("es", "Comida mañana", "¿Quieres comer mañana a las 12:30 en el café de la esquina?"),
    ("es", "Gastos del Q3", "Por favor envía tus gastos del Q3 antes del viernes para evitar retrasos."),
    ("es", "Cumpleaños 70 de mamá", "¿Qué le regalamos a mamá por su 70 cumpleaños? ¿Tienes ideas?"),
    ("es", "Vacaciones Toscana", "Hemos reservado las vacaciones en Toscana para septiembre. Tres semanas."),
    ("es", "Cita médica aplazada", "Mi cita médica fue movida al próximo martes."),
    ("es", "Re: facturas Q3", "Adjunto las facturas pendientes del Q3 para tu aprobación."),
    ("es", "Visita piso Hamburgo", "Visita al piso en Hamburgo-Eppendorf este sábado a las 14h."),
    ("es", "Calefacción rota", "La calefacción no funciona desde anoche. ¿Recomiendas algún fontanero?"),
    ("es", "Programa conferencia adjunto", "Adjunto el programa de la conferencia de Berlín. Jueves plenaria en la Sala Berlín."),
    # Norwegian (10) — short, vocabulary-frugal: BM25 should carry most of the load
    ("no", "Konferanse i Berlin", "Vi møtes neste uke på konferansen i Berlin. Ta med programmet."),
    ("no", "Lunsj i morgen", "Vil du ta lunsj i morgen klokken 12:30 på kafeen på hjørnet?"),
    ("no", "Reiseregning Q3", "Send inn reiseregningen for Q3 innen fredag."),
    ("no", "Mammas 70-årsdag", "Hva gir vi mamma til 70-årsdagen? Har du ideer?"),
    ("no", "Ferie i Toscana", "Vi har booket ferien i Toscana i september. Tre uker."),
    ("no", "Legetime flyttet", "Legetimen min er flyttet til neste tirsdag."),
    ("no", "Re: fakturaer Q3", "Vedlagt åpne fakturaer for Q3 til godkjenning."),
    ("no", "Visning leilighet Hamburg", "Visning av leilighet i Hamburg-Eppendorf på lørdag kl 14."),
    ("no", "Varmen er borte", "Varmen har vært borte siden i natt. Kjenner du en rørlegger?"),
    ("no", "Konferanseprogram vedlagt", "Vedlagt programmet for Berlin-konferansen. Torsdag plenum i sal Berlin."),
    # Japanese (10)
    ("ja", "ベルリン会議", "来週ベルリンで開催される会議でお会いしましょう。アジェンダをお持ちください。"),
    ("ja", "明日のランチ", "明日12時半に角のカフェでランチはどうですか?"),
    ("ja", "第3四半期の経費報告", "金曜日までに第3四半期の経費報告を提出してください。"),
    ("ja", "母の70歳の誕生日", "母の70歳の誕生日に何を贈りましょうか?何かアイデアはありますか?"),
    ("ja", "トスカーナ休暇予約", "9月のトスカーナ休暇を予約しました。3週間です。"),
    ("ja", "通院日変更", "通院日が来週火曜日に変更になりました。"),
    ("ja", "Re: Q3請求書", "Q3の未払い請求書を添付します。承認をお願いします。"),
    ("ja", "ハンブルク内見", "ハンブルク・エッペンドルフのアパート内見は土曜14時です。"),
    ("ja", "暖房故障", "昨夜から暖房が止まっています。配管工をご存知ですか?"),
    ("ja", "会議プログラム添付", "ベルリン会議のプログラムを添付します。木曜午前は本会議場ベルリンにて。"),
]


def build_corpus(conn) -> list[dict[str, Any]]:
    """Insert the synthetic corpus into `accounts` + `messages`; return seed list."""
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO accounts (name, email_address, imap_host, auth_method)"
            " VALUES ('multilingual', 'test@example', 'host', 'password') RETURNING id"
        )
        acct = cur.fetchone()[0]
        out: list[dict[str, Any]] = []
        for i, (lang, subj, body) in enumerate(_SEED):
            sha = bytes([i + 1]) * 32
            cur.execute(
                "INSERT INTO messages (account_id, message_id, raw_sha256, subject,"
                " from_addr, body_text, date_sent, headers, raw_bytes, size_bytes)"
                " VALUES (%s, %s, %s, %s, %s, %s, %s, '{}'::jsonb, %s, %s)"
                " RETURNING id",
                (
                    acct, f"<mc{i}@local>", sha, subj, f"user{i % 5}@example.com",
                    body, _BASE + timedelta(days=i), b"raw", len(body),
                ),
            )
            mid = cur.fetchone()[0]
            out.append({"id": mid, "lang": lang, "subject": subj, "body": body})
    conn.commit()
    return out

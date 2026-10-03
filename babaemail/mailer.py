"""Explicit SMTP delivery with STARTTLS. Importing this module never connects."""
from __future__ import annotations

import os
import smtplib
import ssl
from dataclasses import dataclass
from email.message import EmailMessage
from typing import Mapping

REQUIRED = ('BABAEMAIL_SMTP_HOST', 'BABAEMAIL_SMTP_PORT', 'BABAEMAIL_SMTP_USER',
            'BABAEMAIL_SMTP_PASSWORD', 'BABAEMAIL_FROM', 'BABAEMAIL_TO', 'BABAEMAIL_ALERT_TO')


@dataclass(frozen=True)
class SMTPConfig:
    host: str
    port: int
    user: str
    password: str
    sender: str
    recipient: str
    alert_recipient: str

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> SMTPConfig:
        env = os.environ if environ is None else environ
        missing = [key for key in REQUIRED if not env.get(key, '').strip()]
        if missing:
            raise ValueError('Missing required email environment variables: ' + ', '.join(missing))
        try:
            port = int(env['BABAEMAIL_SMTP_PORT'])
            if not 1 <= port <= 65535:
                raise ValueError
        except ValueError as exc:
            raise ValueError('BABAEMAIL_SMTP_PORT must be an integer from 1 to 65535') from exc
        return cls(env['BABAEMAIL_SMTP_HOST'], port, env['BABAEMAIL_SMTP_USER'],
                   env['BABAEMAIL_SMTP_PASSWORD'], env['BABAEMAIL_FROM'],
                   env['BABAEMAIL_TO'], env['BABAEMAIL_ALERT_TO'])


def send_email(subject: str, html: str, text: str, *, alert: str | None = None,
               config: SMTPConfig | None = None, smtp_factory=None) -> None:
    config = config or SMTPConfig.from_env()
    msg = EmailMessage()
    msg['Subject'], msg['From'], msg['To'] = subject, config.sender, config.recipient
    msg.set_content(text)
    msg.add_alternative(html, subtype='html')
    alert_msg = None
    if alert:
        alert_msg = EmailMessage()
        alert_msg['Subject'] = 'BabaEmail: akcij ni bilo mogoče preveriti'
        alert_msg['From'], alert_msg['To'] = config.sender, config.alert_recipient
        alert_msg.set_content(alert)
    factory = smtp_factory or smtplib.SMTP
    with factory(config.host, config.port, timeout=30) as smtp:
        smtp.ehlo()
        smtp.starttls(context=ssl.create_default_context())
        smtp.ehlo()
        smtp.login(config.user, config.password)
        # Alert first: a failure alert must not be silently skipped after the
        # grandma message has already been delivered.
        if alert_msg:
            smtp.send_message(alert_msg)
        smtp.send_message(msg)

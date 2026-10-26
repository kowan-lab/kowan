"""Notification-channel type constants.

A notification channel is stored as a :class:`~kowan.models.credential.Credential`
of kind ``channel`` (its provider config encrypted at rest, ``enabled``/``minLevel``
in ``meta``). This module keeps only the provider ``type`` constants shared by the
notify package and the credentials API.
"""

from __future__ import annotations

# Notification channel type constants.
ChannelTypeSMTP = "smtp"
ChannelTypeWebhook = "webhook"
ChannelTypeNtfy = "ntfy"
ChannelTypeTelegram = "telegram"
ChannelTypeSlack = "slack"
ChannelTypeDiscord = "discord"

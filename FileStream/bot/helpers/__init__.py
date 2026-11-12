"""Telegram bot helper functions and utilities."""

__all__ = [
    "register_bot_commands",
    "build_admin_help_text",
    "get_sync_client",
    "get_bot_scope",
    "file_visible_to_bot",
    "get_force_sub_trial_usage",
    "resolve_media_source",
    "get_bot_media_cache",
    "is_local_media_path",
    "get_cached_media_file_id",
    "cache_media_file_id",
    "invalidate_cached_media_file_id",
    "reply_with_optional_photo",
    "send_with_optional_photo",
    "get_public_file_context",
    "get_public_folder_context",
    "format_expiry_countdown",
    "describe_public_link_expiry",
    "build_forward_share_text",
    "get_invite_link",
    "get_all_internal_channel_ids",
    "is_internal_service_chat",
    "is_configured_channel",
    "get_known_bot_ids",
    "is_secondary_bot_id",
    "is_user_joined",
    "handle_force_sub_retry",
    "send_msg",
    "get_bot_username",
    "get_bot_name",
    "get_bot_id",
    "build_start_link",
    "build_add_to_group_link",
    "build_policy_url",
    "build_updates_channel_url",
    "build_bot_policy_text",
    "build_bot_legal_text",
    "build_policy_page_context",
]


def __getattr__(name: str):
    if name in ("register_bot_commands", "build_admin_help_text"):
        from . import bot_commands
        return getattr(bot_commands, name)
    if name in (
        "get_sync_client", "get_bot_scope", "file_visible_to_bot", "get_force_sub_trial_usage",
        "resolve_media_source", "get_bot_media_cache", "is_local_media_path", "get_cached_media_file_id",
        "cache_media_file_id", "invalidate_cached_media_file_id", "reply_with_optional_photo",
        "send_with_optional_photo", "get_public_file_context", "get_public_folder_context",
        "format_expiry_countdown", "describe_public_link_expiry", "build_forward_share_text",
        "get_invite_link", "get_all_internal_channel_ids", "is_internal_service_chat",
        "is_configured_channel", "get_known_bot_ids", "is_secondary_bot_id", "is_user_joined",
        "handle_force_sub_retry"
    ):
        from . import bot_utils
        return getattr(bot_utils, name)
    if name == "send_msg":
        from . import broadcast_helper
        return getattr(broadcast_helper, name)
    if name in ("get_bot_username", "get_bot_name", "get_bot_id", "build_start_link", "build_add_to_group_link"):
        from . import client_identity
        return getattr(client_identity, name)
    if name in ("build_policy_url", "build_updates_channel_url", "build_bot_policy_text", "build_bot_legal_text", "build_policy_page_context"):
        from . import legal
        return getattr(legal, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

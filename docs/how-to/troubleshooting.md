# Troubleshooting

If something isn’t working, rerun with debug logging enabled:

```sh
tunapi --debug
```

Then check `debug.log` for errors and include it when reporting issues.

You can also run a preflight check:

```sh
tunapi doctor
```

This validates your Telegram token, chat id, topics setup, file transfer permissions, and voice transcription configuration.

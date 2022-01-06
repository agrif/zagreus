#!/usr/bin/env bash

ttyd --base-path /z80 --index ./html/dist/inline.html -t disableResizeOverlay=true -t disableLeaveAlert=true -t titleFixed=zagreus -- ./local/env/bin/python -m zagreus.client

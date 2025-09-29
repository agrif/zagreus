#!/usr/bin/env bash

ttyd --base-path /z80 --index ./html/dist/inline.html --writable -t disableResizeOverlay=true -t disableLeaveAlert=true -t titleFixed=zagreus -- python -m zagreus.client

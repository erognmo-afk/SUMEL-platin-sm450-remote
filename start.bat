@echo off
rem Starter SUMEL (fjernkontroll for Platin SM450) og aapner den i nettleseren.
cd /d "%~dp0"
title SUMEL
py -m lemus_remote --open
pause

#!/usr/bin/env bash
# Baut flo-mcbridge.jar aus dem Java-Quellcode.
#   - Standard: nutzt javac/jar aus dem PATH (JDK 17+ noetig).
#   - Per Umgebungsvariable JAVA_BIN auf ein eigenes JDK zeigen, z. B.:
#       JAVA_BIN=/pfad/zum/jdk/bin ./build.sh
set -euo pipefail
cd "$(dirname "$0")"

JAVAC="${JAVA_BIN:+$JAVA_BIN/}javac"
JAR="${JAVA_BIN:+$JAVA_BIN/}jar"

echo "Kompiliere mit: $("$JAVAC" -version 2>&1)"
rm -rf build && mkdir -p build
# shellcheck disable=SC2046
"$JAVAC" --release 17 -d build $(find src -name '*.java')
"$JAR" --create --file flo-mcbridge.jar \
       --main-class gg.flo.mcbridge.FloMcBridge -C build .
echo "Fertig: $(pwd)/flo-mcbridge.jar"

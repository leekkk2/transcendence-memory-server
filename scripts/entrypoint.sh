#!/bin/bash
set -e

# 创建数据目录（named volume 首次挂载时为空）
mkdir -p /data/tasks/active /data/tasks/archived /data/tasks/rag/containers
mkdir -p /data/memory /data/memory_archive

echo ""
echo "========================================================"
echo "  Transcendence Memory Server — starting"
echo "========================================================"
echo ""

BUILD_FLAVOR="${TM_BUILD_FLAVOR:-lite}"
echo "  Build Flavor: ${BUILD_FLAVOR}"

# 必需配置检查
missing_required=0
if [ -z "$RAG_API_KEY" ]; then
    echo "  [!!] RAG_API_KEY not set (API authentication disabled)"
    missing_required=1
fi
if [ -z "$EMBEDDING_API_KEY" ]; then
    echo "  [!!] EMBEDDING_API_KEY not set (vector search disabled)"
    missing_required=1
else
    echo "  [OK] EMBEDDING_API_KEY configured"
fi

# 可选功能检查
if [ -n "$LLM_API_KEY" ]; then
    echo "  [OK] LLM_API_KEY configured -> LightRAG knowledge graph enabled"
else
    echo "  [--] LLM_API_KEY not set -> LightRAG disabled"
fi
if [ -n "$VLM_API_KEY" ]; then
    echo "  [OK] VLM_API_KEY configured -> Multimodal RAG enabled"
else
    echo "  [--] VLM_API_KEY not set -> Multimodal RAG disabled"
fi
if [ "$BUILD_FLAVOR" = "lite" ] && [ -n "$VLM_API_KEY" ]; then
    echo "  [WARN] Multimodal is configured while running the lite build"
fi

echo ""
if [ "$missing_required" -eq 0 ] && [ "$BUILD_FLAVOR" = "full" ] && [ -n "$LLM_API_KEY" ] && [ -n "$VLM_API_KEY" ]; then
    echo "  -> Architecture: rag-everything (full)"
elif [ "$missing_required" -eq 0 ] && [ -n "$LLM_API_KEY" ]; then
    echo "  -> Architecture: lancedb+lightrag"
else
    echo "  -> Architecture: lancedb-only"
fi
echo ""
echo "========================================================"
echo ""

# 启动服务（exec 替换当前进程，PID 1 = uvicorn，正确处理信号）
exec uvicorn task_rag_server:app --app-dir /app/scripts --host 0.0.0.0 --port 8711

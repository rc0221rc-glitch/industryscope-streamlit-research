# Streamlit Cloud 部署说明

## GitHub 仓库

- Repository: https://github.com/rc0221rc-glitch/industryscope-streamlit-research
- Owner: `rc0221rc-glitch`
- Repo: `industryscope-streamlit-research`
- Branch: `main`
- Main file path: `app.py`
- 当前可见性：Public

## 部署入口

打开：

https://share.streamlit.io/deploy

填写：

- Repository: `rc0221rc-glitch/industryscope-streamlit-research`
- Branch: `main`
- Main file path: `app.py`

如果页面支持预填参数，也可以尝试：

https://share.streamlit.io/deploy?owner=rc0221rc-glitch&repo=industryscope-streamlit-research&branch=main&mainModule=app.py

## Secrets

在 Streamlit Cloud 的 App -> Settings -> Secrets 中粘贴以下内容，并把空值补齐：

```toml
CAPTION_SUFFIX = ""

OPENAI_API_KEY = ""
DEEPSEEK_API_KEY = ""
QWEAPI_AUTH_TOKEN = ""
ANTHROPIC_AUTH_TOKEN = ""
OPENAI_COMPAT_API_KEY = ""

QWEAPI_BASE_URL = "https://qweapi.com"
QWEAPI_MODEL = "claude-opus-4-8"
QWEAPI_MODEL_DEEP = "claude-opus-4-8[1M]"
DEEPSEEK_BASE_URL = "https://api.deepseek.com"
OPENAI_COMPAT_BASE_URL = ""
OPENAI_COMPAT_MODEL = ""
```

## 注意事项

1. 不要把真实 API Key 提交到 GitHub。
2. 当前仓库是 public，可直接在 Streamlit Cloud 选择 GitHub 仓库部署。
3. `scrapling` 只使用轻量 Selector 正文抽取层，不默认启用 Playwright/动态浏览器抓取。
4. 如果部署时依赖安装失败，可先移除 `requirements.txt` 中的可选依赖 `trafilatura` 或 `scrapling`，工具会回退到 BeautifulSoup 正文抽取。
5. 部署完成后，Streamlit 会给出类似 `https://xxx.streamlit.app` 的长期访问链接。

# vendored 静态资源

## echarts.min.js

| 项 | 值 |
|---|---|
| 版本 | **6.1.0** |
| 下载 URL | `https://cdn.jsdelivr.net/npm/echarts@6.1.0/dist/echarts.min.js` |
| SHA-256 | `b66b25aeb4df84e33199dc21694014d336d222cbd9deb0e5a7c14bd6aa0d0fd0` |
| 大小 | 1,121,883 bytes |
| 许可 | Apache-2.0（文件头保留原始 license banner，**不要删**） |

### 为什么 vendor 而不是用 npm 依赖

**报告必须完全自包含。** 它要能当 CI artifact 传递、作邮件附件、被离线打开，
所以打开时不能产生任何网络请求。这就要求 ECharts 的源码**内联进 HTML**，
而不是 `<script src="...cdn...">`。

既然要内联，它就必须在仓库里 —— npm 依赖在 `node_modules` 里（不入库），
构建期还需要 Node 工具链；而本项目是纯 Python 的，为了一张图引入前端构建
是不划算的。

`tests/report/test_html.py::test_no_external_resource_is_referenced`
与 `test_missing_vendored_echarts_fails_loudly` 一起守住这个性质：
前者断言渲染出的页面没有任何会发起网络请求的引用，
后者确保文件缺失时**报错**而不是悄悄退回 CDN。

### 升级步骤

```bash
cd "C:/Users/17207/Desktop/评测harness"
NEW=6.2.0   # 改成目标版本
curl -sS -L -o src/harness/static/echarts.min.js \
  "https://cdn.jsdelivr.net/npm/echarts@${NEW}/dist/echarts.min.js"
sha256sum src/harness/static/echarts.min.js     # 更新本文件里的哈希与新版本号
uv run pytest tests/report/ -q
```

升级后务必**在浏览器里打开一次报告**，确认三张图仍然渲染。
自动化测试只能证明"没有外部引用"，证明不了"图还画得出来"。

### 一个容易踩的坑

`echarts.min.js` 里含 `http://www.w3.org/2000/svg` 这类字符串 ——
它们是 **XML 命名空间标识符**（SVG 规范要求的常量），不是资源引用，
浏览器永远不会去请求。

所以**不要**写 `assert "http://" not in html` 这种断言：它在 vendor 之后
必然失败，而且它测的不是我们真正在意的性质（见上面那两条测试）。

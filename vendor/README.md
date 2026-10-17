# 第三方代码说明

本目录存放**内联进组件的外来代码**。为保证可追溯性，每个文件都记录来源与许可证。

---

## glasatarjs.core.js

| 项 | 值 |
|---|---|
| 项目 | [jimhill/glasatarjs](https://github.com/jimhill/glasatarjs) |
| 用途 | WebGL 磨砂玻璃语音可视化（本项目面试官语音动效的渲染核心） |
| 许可证 | **MIT** |
| 版本 | 取自 `main` 分支的 `core.js`（约 46 KB） |
| 取用日期 | 2026-10-17 |
| 本地文件 | `glasatarjs.core.js` |

**为什么内联**：本项目的动效组件是 `st.iframe` 里的自包含 HTML，独立文档无法
跨文档 `import` 外部模块，因此把源码直接嵌进 HTML 模板。

**为什么选它**：本项目需要「音频驱动的语音动效」。glasatarjs 提供现成的
WebGL 玻璃扭曲着色器与中心光球，且核心是**原生 JS**（不需要 React、不需要构建流程），
可直接内联使用。

### 本项目对源码做的唯一改动

把结尾的 ESM 导出改写为挂载到 `window`：

```diff
- export { AudioAnalyzer, GlastarJS };
+ window.AudioAnalyzer = AudioAnalyzer; window.GlastarJS = GlastarJS;
```

原因：内联在 `<script>`（非 `type="module"`）里不能使用 `export` 语句。
**着色器、渲染逻辑与所有配置项均未改动。**

### MIT 许可证原文

上游仓库根目录有 `LICENSE`（MIT）。按 MIT 要求保留版权声明：

```
MIT License

Copyright (c) 2025 Jim Hill

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

> 注：上游 `core.js` 构建产物中**不含**版权注释，此处保留声明以满足 MIT 的
> "copies or substantial portions" 要求。若上游版权年份有出入，以其 `LICENSE` 为准。

### 已知局限（本项目已实测）

1. **`<audio>.captureStream()` 拿不到音频轨**（轨道数 0），必须改用
   `AudioContext → MediaStreamDestination` 才能驱动它。
2. **不支持真透明背景**：片元着色器里写死 `finalAlpha = u_opacity`，
   且背景 rgb 被乘上玻璃色。本项目改用「同色底」（背景色 = 页面纸面色）。

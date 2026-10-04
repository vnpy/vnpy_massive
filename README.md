# VeighNa 框架的 Massive 数据服务接口

<p align="center">
  <img src ="https://vnpy.oss-cn-shanghai.aliyuncs.com/vnpy-logo.png"/>
</p>

<p align="center">
    <img src ="https://img.shields.io/badge/version-1.0.0-blueviolet.svg"/>
    <img src ="https://img.shields.io/badge/platform-windows|linux|macos-yellow.svg"/>
    <img src ="https://img.shields.io/badge/python-3.10|3.11|3.12|3.13-blue.svg" />
    <img src ="https://img.shields.io/github/license/vnpy/vnpy.svg?color=orange"/>
</p>

## 说明

基于 Massive REST API，使用 requests 直接调用，支持美股市场的 K 线数据（股票、指数、期权）。

注意：
- 需要购买相应的数据服务权限。
- 指数、期权的 ticker 前缀（如 I:、O:）由 datafeed 自动补全。指数自动补 `I:` 的代码只有 SPX、NDX、RUT、DJX、VIX。


## 安装

安装环境推荐基于4.0.0版本以上的【[**VeighNa Studio**](https://www.vnpy.com)】。

直接使用pip命令：

```
pip install vnpy_massive
```


或者下载源代码后，解压后在cmd中运行：

```
pip install .
```


## 使用

在 VeighNa 中使用 Massive 时，需要在全局配置中填写以下字段信息：

| 名称 | 含义 | 必填 | 举例 |
|------|------|------|------|
| datafeed.name | 名称 | 是 | massive |
| datafeed.password | 密码 | 是 | (API Key) |

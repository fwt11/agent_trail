"""Agent 足迹 系统托盘（仅 Linux；Windows/macOS 后端规划中）。

app.py 是托盘主体（子进程管理 + GTK 窗口 + 菜单）；sni.py 是原生
StatusNotifierItem 实现。托盘不 import 包内其他模块，因此可以由
cli 的重执行机制在带 GI 绑定的系统 Python 上运行。
"""

# Word Master

一个基于 **PyQt5** 的桌面端单词管理与记忆学习项目。

## 当前仓库内容

当前仓库已上传核心源码：

- `main.py`：主程序入口与主要界面逻辑
- `requirements.txt`：Python 依赖
- `.gitignore`：Git 忽略规则

## 项目特性

从源码中可以看出，本项目包含以下功能：

- 单词本（WordBook）与单词条目（WordEntry）管理
- 本地数据持久化与兼容旧格式数据迁移
- 全局词库与错词统计
- 多种学习算法切换
  - `normal`
  - `ebbinghaus`
  - `scientific`
- 学习状态记录与复习流程
- 撤销 / 恢复（undo / redo）
- 基于 PyQt5 的图形界面

## 运行环境

- Python 3.10+
- PyQt5

## 安装依赖

```bash
pip install -r requirements.txt
```

## 运行项目

```bash
python main.py
```

## 说明

源码中引用了以下资源文件：

- `word_master.png`
- `word_master.ico`

如果后续你本地还有这些图标或启动图资源，可以继续补传到仓库根目录，这样界面显示会更完整。

另外，程序运行后会在项目目录下生成或读取本地数据文件，例如：

- `word_data.wmz`
- `word_data.json`

这些通常属于运行时数据，不建议提交到仓库。

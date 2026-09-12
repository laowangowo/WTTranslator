# WTTranslator
基于8111接口的战争雷霆翻译器  
[官网](https://wt.ngup.eu.org/) | [QQ群:1108421163](https://qun.qq.com/universal-share/share?ac=1&authKey=uqp4DUkAT6RjSS7xEnoQ3jtAg8olH5FU%2BkSoNddr%2FShzwclICACwUU6WZWdX%2F6AJ&busi_data=eyJncm91cENvZGUiOiIxMTA4NDIxMTYzIiwidG9rZW4iOiJQRjdLY2RWSW82OE9NL3dEWVNNYlpNY0YvWHU4SDJ2U3Z0WlJtc1hrQzMrVkpuZUpReUZTQUVncWZNdDRrdWExIiwidWluIjoiNjQ5ODE1MjM1In0%3D&data=vIjuCpO9sAIaKgQRIg7UN8G0odBAO3aeYHIk52gcngS7gzCYwCsWSxtv4EOoEVx05vbjZsiuNYBKqGuJBDMsZw&svctype=4&tempid=h5_group_info) | [Microsoft Store](?仍在审核)
> [!IMPORTANT]
> 注意：本人不保证使用翻译器绝对不会导致封号，亦不对您因使用翻译器所产生的一切不良后果承担任何责任。
## 功能
- AI翻译
  - 使用HY-MT2 1.8B量化模型本地翻译
  - 使用SenseVoiceSmall模型进行语音输入识别
- AI生成回复(Beta)
- 中译英
  - 支持愤怒模式(翻译输出可能会带不文明词汇)
- 守护进程
  - 随雷自启

## 安装
1. 前往[官网](https://wt.ngup.eu.org/)或[Release])下载最新版安装包
2. 启动安装包并安装
3. 启动WT Translator

## 构建
使用`Pyinstaller`将`main.py`和`guardian.py`打包即可

## 隐私政策
WT Translator会上报使用数据，包括(设备ID)，不包含聊天内容、账号信息、 设备硬件信息或可定位到个人的资料。

## 目录结构
```
WTTranslator/
├── main.py             #入口
├── guardian.py         #守护进程
├── wt_translator/
│   ├── blacklist.py    #消息黑名单
│   ├── chat_source.py  #聊天数据请求
│   ├── config.py       #配置文件处理
│   ├── glossary.py     #术语表
│   ├── quickmenu.py    #快捷回复
│   ├── reporter.py     #使用统计
│   ├── translator.py   #AI翻译
│   ├── ui.py           #UI界面
│   ├── updater.py      #更新检查器
│   ├── voice.py        #语音输入
│   └── __init__.py     #核心包
├── config.json         #配置文件
├── config.yml          #版本配置文件
├── glossary.json       #术语表配置文件
└── setting.json        #用户设置
```

## 说明
- 数据来源为战争雷霆官方内置的8111接口
- **免责声明**: 使用本软件所造成的一切不良后果由用户自行承担

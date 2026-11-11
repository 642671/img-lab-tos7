# 项目结构

```text
img-lab-tos7/
├── VERSION                         # 当前版本：0.1.0
├── config.ini                      # TOS 应用中心 iframe 元数据
├── img-lab.lang                    # 14 语言名称、描述、发布说明
├── img-lab.env                     # systemd 环境变量
├── DEBIAN/
│   ├── control                     # 包名、版本、架构、依赖、维护者
│   ├── postinst                    # 创建用户、解压前端、安装并启动服务
│   ├── prerm                       # 有界停止、禁用、清理 socket
│   └── postrm                      # 移除 unit 和应用目录，purge 时移除用户
├── init.d/
│   └── imglab-system.service       # systemd unit
├── images/icons/
│   └── img-lab.svg                 # 64×64 应用图标
├── backend/
│   ├── img-lab-service             # systemd 启动入口
│   ├── img_lab/
│   │   ├── __init__.py             # 版本与应用常量
│   │   ├── content.py              # 确定性渲染与下采样探针
│   │   ├── encoders.py             # PNG 编码、libjpeg/libwebp ctypes 绑定
│   │   ├── sizing.py               # 目标校验、两级体积求解器
│   │   ├── library.py              # 图片库、CSV、ZIP、清理
│   │   ├── speedtest.py            # HTTP 测速与分阶段计时
│   │   └── server.py               # HTTP 路由、静态文件、socket 生命周期
│   └── tests/
│       ├── test_content.py         # 渲染、确定性、探针几何
│       ├── test_encoders.py        # PNG 往返、JPEG/WebP 容器与质量
│       ├── test_sizing.py          # 标定基准、体积收敛、取消
│       ├── test_library.py         # 持久化、CSV 字节、ZIP、清理
│       ├── test_speedtest.py       # URL 校验、计时头、本地 HTTP
│       ├── test_server_helpers.py  # 路由、任务、图片库、预览、CORS
│       └── test_package_contract.py# 版本、语言、unit、生命周期静态检查
├── frontend/
│   ├── index.html                  # 应用页面
│   ├── app.js                      # API 探测、表单、轮询、渲染
│   ├── theme.js                    # TOS 主题继承
│   ├── tools.js                    # 格式化、先验估算、规格解析
│   ├── styles.css                  # 主题、布局、响应式样式
│   └── favicon.svg                 # 浏览器标签图标
├── tests/
│   └── frontend_tools.test.js      # 前端 Node 单元测试
├── docs/
│   ├── PROJECT_LAYOUT.md           # 本文件
│   └── TOS7_SPEC_COMPLIANCE_CN.md  # TOS 7 规范对照与实机验收
├── build.py                        # 确定性 Deb 构建与内容校验
├── README.md                       # 使用、安装、构建、限制
├── CHANGELOG.md                    # 版本记录
├── CLAUDE.md                       # 后续自动化开发约束
├── LICENSE                         # MIT
├── .gitignore
└── .gitattributes
```

## 运行时安装布局

```text
/usr/local/img-lab/
├── config.ini
├── img-lab.lang
├── img-lab.env
├── LICENSE
├── bin/
│   └── img-lab-service
├── lib/
│   └── img_lab/
│       ├── __init__.py
│       ├── content.py
│       ├── encoders.py
│       ├── sizing.py
│       ├── library.py
│       ├── speedtest.py
│       └── server.py
├── images/icons/
│   └── img-lab.svg
├── init.d/
│   └── imglab-system.service
├── webui.bz2                       # 构建产物，postinst 解压
├── webui/                          # postinst 生成
├── data/
│   ├── library/
│   │   ├── index.json              # 图片库索引
│   │   └── <id>.<ext>              # 生成的图片
│   └── img-lab-bundle-*.zip        # 临时打包文件，响应后删除
└── logs/                           # postinst-diagnostic.txt
```

共享路径：

```text
/etc/systemd/system/imglab-system.service
/var/api/img-lab.sock
```

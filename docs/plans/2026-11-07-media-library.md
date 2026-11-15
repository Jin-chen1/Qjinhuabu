# Q金固定素材库实施计划

**目标：** 本机启动时创建或恢复同一个“Q金素材库”，之后将新增素材文件作为附件保存到其中。

**设计：** macOS 用户数据目录保存固定 Base 绑定、SQLite 补传队列和附件快照，独立于源码安装目录。后台扫描应用 output 中完成写入的图片、视频和音频；原生参考图在临时文件删除前加入队列。云端写入串行执行，按稳定素材标识恢复记录和附件，避免重复创建。

**技术：** Python、SQLite、portalocker、用户身份 lark-cli、FastAPI、React Query。

- [x] `tests/test_media_library.py`：先验证首次创建、重启复用、权限错误不建新表、创建回执丢失、素材快照、未知写入防重与跨重启补传；运行确认缺少实现。
- [x] `doubao_work/media_library_client.py`：封装 Base 搜索、创建、schema 验证、精确素材查询、记录写入、附件上传；保留用户身份，不输出凭据或上游错误正文。
- [x] `doubao_work/media_library.py`：持久绑定、跨进程锁、SQLite outbox、稳定文件扫描、快照和串行同步；未确认的写入只核验原对象。
- [x] `doubao_work/media_library_runtime.py`：本机 CE 启动及关闭、参考图归档；`api/app.py` 接生命周期，`jobs.py` 接参考图，`start-ce.sh` 启用本机功能。
- [x] `api/routes/doubao_work.py`：沿用本机来源保护，提供状态及唤醒补传；`local-doubao-settings.tsx` 显示原表链接、已归档/待同步及重试。
- [x] 验证：58项后端测试、55项设置测试、ruff、Shell语法、TypeScript/CE构建；真实图片/视频附件下载SHA256、初始化及实际重启复用同一 Base；未修改用户画布。
- [x] 更新使用文档与已有 Obsidian 项目恢复笔记；不提交 Git，不删除本地预览。

**边界：** 首次启用只自动归档新增素材，已有素材保持原样；新机器需要本人飞书授权。绑定存在时权限/网络/删除错误都保留原绑定并提示恢复，不自动另建 Base。云端回执未知时停止该素材写入，只有核验到原记录或附件后继续；不能把一次空查询当作写入未发生。文件单个上限2GB；本机缓存继续承担即时预览与失败补传。

# CICT Deployment

- [部署文档](./deployment-guide.md)
- [Ubuntu 直接安装器](./installer/README.md)

## Ubuntu 构建安装器

```bash
cd installer
./build-binary-native.sh
./package.sh
```

生成的单文件二进制和 tar 包位于 `installer/dist/`。目标 Ubuntu 不需要安装 Python；构建机需要 Python、pip 和 PyInstaller，或者使用 `./build-binary.sh` 通过 Docker 构建。

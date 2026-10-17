# 参与贡献 / Contributing

欢迎提 issue 和 pull request。

## 许可

本项目以 [GNU AGPL-3.0](LICENSE) 发布。你提交的贡献也以 AGPL-3.0 授权，和项目其他代码一样。

## DCO 签名（必需）

每个 commit 都需要带 DCO（Developer Certificate of Origin）签名，表示你有权以本项目的许可证提交这段代码。
DCO 1.1 全文见 <https://developercertificate.org/>。

提交时加 `-s`，git 会自动在 commit 信息末尾加上签名行：

```
git commit -s -m "说明这次改了什么"
```

签名行的格式是 `Signed-off-by: 你的名字 <你的邮箱>`，名字和邮箱取自 `git config user.name` / `user.email`。
忘了签的话，可以用 `git commit --amend -s`（最近一个 commit）或 `git rebase --signoff`（多个 commit）补上。

## 不需要 CLA

本项目不要求签署任何 CLA（贡献者许可协议），DCO 签名就够了。

---

**English:** Contributions are welcome under AGPL-3.0. Every commit must carry a DCO sign-off (`git commit -s`); see the DCO 1.1 text at <https://developercertificate.org/>. No CLA is required.

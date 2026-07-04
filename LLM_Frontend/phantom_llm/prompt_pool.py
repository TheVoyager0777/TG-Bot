"""
prompt_pool.py — 每个 worker（及 orchestrator）的可复用提示词池。

支持按 owner 保存/列出/编辑/删除命名提示词片段，并可直接调用（文本作为 prompt
发送给对应 worker）。持久化为 JSON 文件，重启后不丢失。
另有全局共享池（key="*"），其条目对所有 worker 可见（own 同名条目优先覆盖）。
"""
from __future__ import annotations

import json
import logging
import os
import tempfile
import time
from typing import Any

log = logging.getLogger("tgclaude.prompts")

SHARED = "*"
MAIN = "main"

# ---------- 类型别名 ----------
_Entry = dict[str, Any]  # {"text": str, "created": float, "updated": float}
_Pool = dict[str, _Entry]  # name -> entry


def default_seed() -> dict[str, _Pool]:
    """返回一份初始共享池，供首次初始化时填充 '*'。"""
    now = time.time()

    def _e(text: str) -> _Entry:
        return {"text": text, "created": now, "updated": now}

    return {
        SHARED: {
            "代码审查": _e("审查最近的改动，找出 bug、安全问题与可维护性问题，给出修复建议。"),
            "写测试": _e("为刚才改动的代码补充单元测试并运行。"),
            "总结进度": _e("用中文简要总结你当前的进度、遇到的问题和下一步。"),
        }
    }


class PromptPool:
    """提示词池：按 owner 管理命名 prompt 片段，持久化到 JSON。"""

    def __init__(self, path: str):
        self._path = path
        self._data: dict[str, _Pool] = {}
        self._load()

    # ---- 公开 API ----

    def list(self, owner: str, *, include_shared: bool = True) -> list[dict]:
        """列出 owner 的提示词（含可选共享池），own 同名覆盖 shared。"""
        own: _Pool = self._data.get(owner, {})
        results: list[dict] = []
        for name in sorted(own):
            results.append(self._export(own[name], name, "own"))
        if include_shared and owner != SHARED:
            shared: _Pool = self._data.get(SHARED, {})
            for name in sorted(shared):
                if name not in own:
                    results.append(self._export(shared[name], name, "shared"))
        return results

    def get(self, owner: str, name: str, *, include_shared: bool = True) -> dict | None:
        """按名称查找：own 优先，其次 shared。"""
        own = self._data.get(owner, {})
        if name in own:
            return self._export(own[name], name, "own")
        if include_shared and owner != SHARED:
            shared = self._data.get(SHARED, {})
            if name in shared:
                return self._export(shared[name], name, "shared")
        return None

    def save(self, owner: str, name: str, text: str) -> dict:
        """创建或覆盖条目。返回存储后的条目。"""
        name = self._validate_name(name)
        text = text.strip()
        if not text:
            raise ValueError("提示词内容不能为空")
        pool = self._data.setdefault(owner, {})
        now = time.time()
        if name in pool:
            pool[name]["text"] = text
            pool[name]["updated"] = now
        else:
            pool[name] = {"text": text, "created": now, "updated": now}
        self._save()
        return self._export(pool[name], name, "own")

    def rename(self, owner: str, old: str, new: str) -> bool:
        """重命名条目。old 不存在或 new 已存在则返回 False。"""
        new = self._validate_name(new)
        pool = self._data.get(owner, {})
        if old not in pool or new in pool:
            return False
        pool[new] = pool.pop(old)
        pool[new]["updated"] = time.time()
        self._save()
        return True

    def delete(self, owner: str, name: str) -> bool:
        """删除 owner 自有池中的条目。"""
        pool = self._data.get(owner, {})
        if name not in pool:
            return False
        del pool[name]
        if not pool:
            del self._data[owner]
        self._save()
        return True

    def rename_owner(self, old_owner: str, new_owner: str) -> None:
        """worker 改名时迁移其池。"""
        if old_owner in self._data:
            self._data[new_owner] = self._data.pop(old_owner)
            self._save()

    def drop_owner(self, owner: str) -> None:
        """删除某 owner 的全部池（永远不删 '*'）。"""
        if owner == SHARED:
            return
        if owner in self._data:
            del self._data[owner]
            self._save()

    def owners(self) -> list[str]:
        """返回所有拥有至少一个条目的 owner key。"""
        return [k for k in self._data if self._data[k]]

    def stats(self) -> dict:
        """快速统计。"""
        total = sum(len(p) for p in self._data.values())
        return {"owners": len(self._data), "total": total}

    # ---- 内部 ----

    @staticmethod
    def _validate_name(name: str) -> str:
        name = name.strip()
        if not name:
            raise ValueError("提示词名称不能为空")
        if "\n" in name or "\r" in name:
            raise ValueError("提示词名称不能包含换行符")
        if len(name) > 64:
            raise ValueError("提示词名称不能超过 64 个字符")
        return name

    @staticmethod
    def _export(entry: _Entry, name: str, scope: str) -> dict:
        return {
            "name": name,
            "text": entry["text"],
            "created": entry.get("created", 0.0),
            "updated": entry.get("updated", 0.0),
            "scope": scope,
        }

    def _load(self) -> None:
        if not os.path.exists(self._path):
            return
        try:
            with open(self._path, "r", encoding="utf-8") as f:
                raw = json.load(f)
        except (json.JSONDecodeError, OSError) as exc:
            log.warning("提示词池文件损坏或无法读取，已忽略: %s", exc)
            return
        if not isinstance(raw, dict):
            log.warning("提示词池顶层不是 dict，已忽略")
            return
        for owner, pool in raw.items():
            if not isinstance(pool, dict):
                continue
            clean: _Pool = {}
            for name, entry in pool.items():
                if isinstance(entry, dict) and "text" in entry and isinstance(entry["text"], str):
                    clean[name] = {
                        "text": entry["text"],
                        "created": float(entry.get("created", 0)),
                        "updated": float(entry.get("updated", 0)),
                    }
            if clean:
                self._data[owner] = clean

    def _save(self) -> None:
        os.makedirs(os.path.dirname(self._path) or ".", exist_ok=True)
        tmp_fd, tmp_path = tempfile.mkstemp(
            dir=os.path.dirname(self._path) or ".", suffix=".tmp"
        )
        try:
            with os.fdopen(tmp_fd, "w", encoding="utf-8") as f:
                json.dump(self._data, f, indent=2, ensure_ascii=False)
            os.replace(tmp_path, self._path)
        except BaseException:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise


# ---------- smoke test ----------
if __name__ == "__main__":
    import tempfile as _tf

    tmp = _tf.NamedTemporaryFile(suffix=".json", delete=False)
    tmp.close()
    os.unlink(tmp.name)  # 确保从空开始

    pp = PromptPool(tmp.name)

    # 填充共享池
    seed = default_seed()
    for name, entry in seed[SHARED].items():
        pp.save(SHARED, name, entry["text"])

    # worker 自有条目
    pp.save("w1", "部署", "执行部署流程并汇报结果。")
    pp.save("w1", "代码审查", "我自己的审查提示词，覆盖共享。")

    # list: own 优先，shared 中同名被隐藏
    entries = pp.list("w1")
    names = [e["name"] for e in entries]
    assert "部署" in names
    assert "代码审查" in names
    # 代码审查 scope 应为 own（覆盖了 shared）
    cr = next(e for e in entries if e["name"] == "代码审查")
    assert cr["scope"] == "own", f"expected own, got {cr['scope']}"
    # shared 条目也可见（未被覆盖的）
    assert "写测试" in names
    st = next(e for e in entries if e["name"] == "写测试")
    assert st["scope"] == "shared"

    # get
    assert pp.get("w1", "部署")["scope"] == "own"
    assert pp.get("w1", "写测试")["scope"] == "shared"
    assert pp.get("w1", "不存在") is None

    # rename
    assert pp.rename("w1", "部署", "发布") is True
    assert pp.get("w1", "部署") is None
    assert pp.get("w1", "发布") is not None
    assert pp.rename("w1", "发布", "代码审查") is False  # new 已存在

    # delete
    assert pp.delete("w1", "发布") is True
    assert pp.delete("w1", "发布") is False  # 已删

    # stats
    s = pp.stats()
    assert s["owners"] >= 1
    assert s["total"] >= 3

    # drop_owner
    pp.drop_owner("w1")
    assert pp.get("w1", "代码审查", include_shared=False) is None
    # shared 依然在
    assert pp.get("w1", "代码审查")["scope"] == "shared"

    # 验证不能 drop "*"
    pp.drop_owner(SHARED)
    assert pp.stats()["total"] >= 3

    # validation
    try:
        pp.save("w1", "", "text")
        assert False, "should have raised"
    except ValueError:
        pass
    try:
        pp.save("w1", "a" * 65, "text")
        assert False, "should have raised"
    except ValueError:
        pass

    # reload persistence
    pp2 = PromptPool(tmp.name)
    assert pp2.stats()["total"] == pp.stats()["total"]

    os.unlink(tmp.name)
    print("ALL TESTS PASSED")
    print(f"stats: {pp2.stats()}")
    print(f"owners: {pp2.owners()}")

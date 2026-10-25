"""
分数预测模型 — 基于知识追踪 + MLP
依赖: scikit-learn, joblib
"""
import numpy as np
from sklearn.neural_network import MLPRegressor, MLPClassifier
from sklearn.preprocessing import StandardScaler
import joblib
from pathlib import Path


class ScorePredictor:
    """两阶段预测：知识追踪输出掌握向量 → MLP 预测总分和过线概率"""

    PASS_SCORE = 425
    MAX_SCORE = 710

    def __init__(self, model_dir: str = "data/models"):
        self.model_dir = Path(model_dir)
        self.model_dir.mkdir(parents=True, exist_ok=True)
        self.scaler = StandardScaler()
        self.regressor = None
        self.classifier = None
        self.is_trained = False
        # 模型版本与训练元信息（供"越用越准"的可见性展示）
        self.meta: dict = {}
        self.version = 0
        self._meta_path = self.model_dir / "meta.pkl"
        self._meta_mtime = 0.0

    def prepare_features(self, mastery: dict) -> np.ndarray:
        """
        将知识点掌握字典转换为特征向量
        特征：20维掌握概率 + 平均掌握度 + 标准差 + 高掌握比例
        """
        if not mastery:
            return np.array([[0.5] * 23])

        values = sorted(mastery.values(), reverse=True)
        if len(values) < 20:
            values += [0.0] * (20 - len(values))
        else:
            values = values[:20]

        avg = float(np.mean(values))
        std = float(np.std(values))
        high_ratio = sum(1 for v in values if v > 0.7) / len(values)

        features = values + [avg, std, high_ratio]
        return np.array([features])

    def train(self, X: np.ndarray, y_score: np.ndarray,
              y_pass: np.ndarray) -> dict:
        """
        训练预测模型（同步）。
        返回训练集指标，用于与验证集指标一起决定是否晋升。
        """
        X_scaled = self.scaler.fit_transform(X)

        # 小样本时必须关掉早停：early_stopping 会切内部验证集，
        # 训练样本少时会直接抛 "validation set is too small"；
        # 同时把网络缩小，避免十几个样本时严重过拟合。
        small_data = len(X) < 20
        solver_kwargs = {
            "activation": "relu",
            "solver": "adam",
            "max_iter": 1000,
            "random_state": 42,
            "early_stopping": not small_data,
        }
        if small_data:
            solver_kwargs["n_iter_no_change"] = 10 ** 6

        # 回归模型：预测总分
        self.regressor = MLPRegressor(
            hidden_layer_sizes=(16, 8) if small_data else (64, 32),
            **solver_kwargs,
        )
        self.regressor.fit(X_scaled, y_score)

        # 分类模型：预测过线概率
        self.classifier = MLPClassifier(
            hidden_layer_sizes=(8,) if small_data else (32, 16),
            **solver_kwargs,
        )
        try:
            self.classifier.fit(X_scaled, y_pass)
        except ValueError:
            # 样本太少时可能只剩一个类别（全部过线/全部未过线），
            # 分类器无法拟合属正常，回归仍可用。
            self.classifier = None

        self.is_trained = True
        pred = self.regressor.predict(X_scaled)
        return {
            "train_mae": float(np.mean(np.abs(pred - y_score))),
            "samples": int(len(X)),
        }

    def evaluate(self, X: np.ndarray, y_score: np.ndarray,
                 y_pass: np.ndarray) -> dict | None:
        """在给定数据集上评估候选模型（未训练返回 None）"""
        if not self.is_trained or self.regressor is None:
            return None
        X_scaled = self.scaler.transform(X)
        pred = self.regressor.predict(X_scaled)
        err = np.abs(pred - y_score)
        return {
            "mae": float(np.mean(err)),
            "rmse": float(np.sqrt(np.mean(err ** 2))),
            "max_error": float(np.max(err)),
            "samples": int(len(X)),
        }

    def predict(self, mastery: dict) -> dict:
        """预测总分和过线概率"""
        features = self.prepare_features(mastery)

        if not self.is_trained:
            return self._heuristic_predict(mastery)

        X_scaled = self.scaler.transform(features)

        score = float(self.regressor.predict(X_scaled)[0])
        score = max(290, min(710, score))

        try:
            if self.classifier is not None:
                proba = float(self.classifier.predict_proba(X_scaled)[0][1])
            else:
                proba = 1 / (1 + np.exp(-(score - 425) / 50))
        except Exception:
            proba = 1 / (1 + np.exp(-(score - 425) / 50))

        return {
            "predicted_score": int(score),
            "pass_probability": round(proba, 3),
            "model": "mlp",
            "model_version": self.version,
            "training_samples": int(self.meta.get("sample_count", 0)),
            "model_mae": self.meta.get("val_mae") or self.meta.get("train_mae"),
        }

    def _heuristic_predict(self, mastery: dict) -> dict:
        """未训练模型时的启发式预测"""
        if not mastery:
            return {"predicted_score": 350, "pass_probability": 0.1,
                    "model": "heuristic"}

        avg_mastery = float(np.mean(list(mastery.values())))
        predicted = int(290 + avg_mastery * 420)
        predicted = max(290, min(710, predicted))
        pass_prob = 1 / (1 + np.exp(-(predicted - 425) / 50))

        return {
            "predicted_score": predicted,
            "pass_probability": round(pass_prob, 3),
            "model": "heuristic",
        }

    def save(self, meta: dict | None = None):
        """
        保存模型。传入 meta 时一并写入版本元信息
        （样本数 / 误差 / 训练时间），供疗效追踪与晋升决策使用。
        """
        # 只存模型权重；未训练时跳过，不覆盖已存在的模型文件
        if self.regressor is not None:
            joblib.dump(self.regressor, self.model_dir / "regressor.pkl")
            joblib.dump(self.scaler, self.model_dir / "scaler.pkl")
            if self.classifier is not None:
                joblib.dump(self.classifier,
                            self.model_dir / "classifier.pkl")
        # 元信息独立于模型文件写入（即使只保存元数据也应生效）
        if meta is not None:
            self.meta = meta
            self.version = int(meta.get("version", self.version))
            joblib.dump(meta, self._meta_path)
            try:
                self._meta_mtime = self._meta_path.stat().st_mtime
            except Exception:
                self._meta_mtime = 0.0

    def load(self) -> bool:
        try:
            self.regressor = joblib.load(self.model_dir / "regressor.pkl")
            classifier_path = self.model_dir / "classifier.pkl"
            self.classifier = (joblib.load(classifier_path)
                               if classifier_path.exists() else None)
            self.scaler = joblib.load(self.model_dir / "scaler.pkl")
            if self._meta_path.exists():
                self.meta = joblib.load(self._meta_path)
                self.version = int(self.meta.get("version", 0))
                self._meta_mtime = self._meta_path.stat().st_mtime
            self.is_trained = True
            return True
        except Exception:
            self.is_trained = False
            return False

    def reload_if_newer(self) -> bool:
        """
        热更新：后台训练产出新版本后，运行中的实例自动加载最新模型，
        无需重启服务（worker 守护着进程，模型文件可能已被替换）。
        """
        try:
            if not self._meta_path.exists():
                return self.load() if not self.is_trained else False
            mtime = self._meta_path.stat().st_mtime
            if mtime != self._meta_mtime:
                return self.load()
        except Exception:
            return False
        return False
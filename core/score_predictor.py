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

    def train(self, X: np.ndarray, y_score: np.ndarray, y_pass: np.ndarray):
        """训练预测模型"""
        X_scaled = self.scaler.fit_transform(X)

        # 回归模型：预测总分
        self.regressor = MLPRegressor(
            hidden_layer_sizes=(64, 32),
            activation='relu',
            solver='adam',
            max_iter=1000,
            random_state=42,
            early_stopping=True,
        )
        self.regressor.fit(X_scaled, y_score)

        # 分类模型：预测过线概率
        self.classifier = MLPClassifier(
            hidden_layer_sizes=(32, 16),
            activation='relu',
            solver='adam',
            max_iter=1000,
            random_state=42,
            early_stopping=True,
        )
        self.classifier.fit(X_scaled, y_pass)

        self.is_trained = True
        self.save()

    def predict(self, mastery: dict) -> dict:
        """预测总分和过线概率"""
        features = self.prepare_features(mastery)

        if not self.is_trained:
            return self._heuristic_predict(mastery)

        X_scaled = self.scaler.transform(features)

        score = float(self.regressor.predict(X_scaled)[0])
        score = max(290, min(710, score))

        try:
            proba = float(self.classifier.predict_proba(X_scaled)[0][1])
        except Exception:
            proba = 1 / (1 + np.exp(-(score - 425) / 50))

        return {
            "predicted_score": int(score),
            "pass_probability": round(proba, 3),
            "model": "mlp",
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

    def save(self):
        if self.regressor:
            joblib.dump(self.regressor, self.model_dir / "regressor.pkl")
        if self.classifier:
            joblib.dump(self.classifier, self.model_dir / "classifier.pkl")
        joblib.dump(self.scaler, self.model_dir / "scaler.pkl")

    def load(self):
        try:
            self.regressor = joblib.load(self.model_dir / "regressor.pkl")
            self.classifier = joblib.load(self.model_dir / "classifier.pkl")
            self.scaler = joblib.load(self.model_dir / "scaler.pkl")
            self.is_trained = True
        except Exception:
            self.is_trained = False
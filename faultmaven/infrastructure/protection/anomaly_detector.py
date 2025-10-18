# File: faultmaven/infrastructure/protection/anomaly_detector.py

import asyncio
import logging
import pickle
import numpy as np
from typing import Dict, List, Optional, Any, Tuple
from datetime import datetime, timezone, timedelta
from collections import deque
import json
import os

# ML imports with graceful fallbacks
try:
    from sklearn.ensemble import IsolationForest
    from sklearn.preprocessing import StandardScaler
    from sklearn.cluster import DBSCAN
    from sklearn.decomposition import PCA
    SKLEARN_AVAILABLE = True
except ImportError:
    SKLEARN_AVAILABLE = False
    logging.warning("scikit-learn not available, using fallback anomaly detection")

from faultmaven.models.behavioral import (
    BehaviorVector, AnomalyResult, AnomalyType, BehaviorProfile,
    TemporalAnomaly
)


class ModelFeedback:
    """Feedback for model improvement"""
    def __init__(self, prediction_id: str, actual_outcome: str, confidence: float):
        self.prediction_id = prediction_id
        self.actual_outcome = actual_outcome  # "true_positive", "false_positive", etc.
        self.confidence = confidence
        self.timestamp = datetime.now(timezone.utc)


class AnomalyExplanation:
    """Explanation for an anomaly detection"""
    def __init__(self, features: Dict[str, float], thresholds: Dict[str, float]):
        self.features = features
        self.thresholds = thresholds
        self.contributing_features = []
        self.explanation_text = ""
        
        # Find contributing features
        for feature, value in features.items():
            threshold = thresholds.get(feature, 0.0)
            if abs(value) > threshold:
                self.contributing_features.append(feature)
        
        # Generate explanation
        if self.contributing_features:
            self.explanation_text = f"Anomaly detected due to unusual values in: {', '.join(self.contributing_features)}"
        else:
            self.explanation_text = "Anomaly detected based on overall pattern deviation"


class AnomalyDetectionSystem:
    """
    Machine learning-based anomaly detection for client behavior
    
    Features:
    - Multiple detection algorithms (Isolation Forest, Statistical, Clustering)
    - Online learning capabilities
    - Explainable detection results
    - Adaptive thresholds
    - Model persistence and loading
    """

    def __init__(self, model_path: Optional[str] = None, enable_online_learning: bool = True):
        self.logger = logging.getLogger(__name__)
        self.model_path = model_path or "/tmp/faultmaven_ml_models"
        self.enable_online_learning = enable_online_learning
        
        # Ensure model directory exists
        os.makedirs(self.model_path, exist_ok=True)
        
        # Model components
        self.isolation_forest = None
        self.scaler = None
        self.clustering_model = None
        self.pca_model = None
        
        # Training data buffer for online learning
        self.training_buffer = deque(maxlen=10000)
        self.feedback_buffer = deque(maxlen=1000)
        
        # Model configuration
        self.anomaly_threshold = 0.1  # Threshold for anomaly score
        self.min_training_samples = 100
        self.retrain_interval = timedelta(hours=24)
        self.last_training = None
        
        # Feature statistics for normalization
        self.feature_stats = {}
        self.adaptive_thresholds = {}
        
        # Model performance tracking
        self.model_metrics = {
            "total_predictions": 0,
            "anomalies_detected": 0,
            "false_positives": 0,
            "true_positives": 0,
            "model_accuracy": 0.0
        }
        
        # Initialize models
        if SKLEARN_AVAILABLE:
            self._initialize_ml_models()
            self._load_models()
        else:
            self.logger.warning("ML models not available, using statistical fallback")
        
        self.logger.info("AnomalyDetectionSystem initialized")

    async def detect_anomalies(self, behavior_vector: BehaviorVector) -> AnomalyResult:
        """
        Detect anomalies in behavior vector using multiple methods
        
        Args:
            behavior_vector: Behavioral features to analyze
            
        Returns:
            AnomalyResult with detection details
        """
        try:
            # Extract features
            features = behavior_vector.features
            feature_array = self._vectorize_features(features)
            
            # Perform detection with multiple methods
            isolation_score = await self._isolation_forest_detection(feature_array)
            statistical_score = await self._statistical_detection(features)
            pattern_score = await self._pattern_detection(features)
            
            # Combine scores
            overall_score = (isolation_score + statistical_score + pattern_score) / 3.0
            
            # Determine anomaly types
            anomaly_types = []
            if isolation_score > self.anomaly_threshold:
                anomaly_types.append(AnomalyType.STATISTICAL_OUTLIER)
            if statistical_score > self.anomaly_threshold:
                anomaly_types.append(AnomalyType.FREQUENCY_ANOMALY)
            if pattern_score > self.anomaly_threshold:
                anomaly_types.append(AnomalyType.PATTERN_ANOMALY)
            
            # Generate explanation
            explanation = self._generate_explanation(features, overall_score)
            
            # Create result
            result = AnomalyResult(
                session_id=behavior_vector.extraction_timestamp.isoformat(),  # Use timestamp as temp ID
                overall_score=overall_score,
                anomaly_types=anomaly_types,
                pattern_anomalies={"overall": overall_score},
                feature_contributions=self._calculate_feature_contributions(features),
                detection_timestamp=datetime.now(timezone.utc),
                model_version="2.0",
                model_confidence=behavior_vector.confidence,
                detection_method="ensemble",
                explanation=explanation.explanation_text,
                recommended_actions=self._get_recommended_actions(overall_score, anomaly_types)
            )
            
            # Update metrics
            self.model_metrics["total_predictions"] += 1
            if overall_score > self.anomaly_threshold:
                self.model_metrics["anomalies_detected"] += 1
            
            # Add to training buffer for online learning
            if self.enable_online_learning:
                await self._add_training_sample(feature_array, overall_score)
            
            return result
            
        except Exception as e:
            self.logger.error(f"Error in anomaly detection: {e}")
            return AnomalyResult(
                session_id="error",
                overall_score=0.0,
                anomaly_types=[],
                detection_timestamp=datetime.now(timezone.utc),
                model_version="2.0",
                model_confidence=0.0,
                detection_method="error_fallback"
            )

    async def train_models(self, historical_data: List[Dict[str, Any]]):
        """
        Train models on historical behavioral data
        
        Args:
            historical_data: List of behavior records with features and labels
        """
        try:
            if not SKLEARN_AVAILABLE:
                self.logger.warning("Cannot train ML models without scikit-learn")
                return
            
            if len(historical_data) < self.min_training_samples:
                self.logger.warning(f"Insufficient training data: {len(historical_data)} < {self.min_training_samples}")
                return
            
            self.logger.info(f"Training anomaly detection models on {len(historical_data)} samples")
            
            # Prepare training data
            X = []
            for record in historical_data:
                features = record.get('features', {})
                feature_vector = self._vectorize_features(features)
                X.append(feature_vector)
            
            X = np.array(X)
            
            # Train scaler
            self.scaler = StandardScaler()
            X_scaled = self.scaler.fit_transform(X)
            
            # Train Isolation Forest
            self.isolation_forest = IsolationForest(
                contamination=0.1,  # Expect 10% anomalies
                random_state=42,
                n_estimators=100
            )
            self.isolation_forest.fit(X_scaled)
            
            # Train clustering model for pattern detection
            self.clustering_model = DBSCAN(eps=0.5, min_samples=5)
            cluster_labels = self.clustering_model.fit_predict(X_scaled)
            
            # Train PCA for dimensionality reduction
            self.pca_model = PCA(n_components=min(10, X.shape[1]))
            self.pca_model.fit(X_scaled)
            
            # Update feature statistics
            self._update_feature_stats(historical_data)
            
            # Update adaptive thresholds
            self._update_adaptive_thresholds(X_scaled)
            
            # Save models
            await self._save_models()
            
            self.last_training = datetime.now(timezone.utc)
            self.logger.info("Model training completed successfully")
            
        except Exception as e:
            self.logger.error(f"Error training models: {e}")

    async def update_online(self, feedback: ModelFeedback):
        """
        Update models with feedback for online learning
        
        Args:
            feedback: Feedback on model predictions
        """
        try:
            # Store feedback for analysis
            self.feedback_buffer.append(feedback)
            
            # Update metrics based on feedback
            if feedback.actual_outcome == "true_positive":
                self.model_metrics["true_positives"] += 1
            elif feedback.actual_outcome == "false_positive":
                self.model_metrics["false_positives"] += 1
            
            # Calculate accuracy
            total_feedback = len(self.feedback_buffer)
            if total_feedback > 0:
                correct = sum(1 for f in self.feedback_buffer 
                            if f.actual_outcome in ["true_positive", "true_negative"])
                self.model_metrics["model_accuracy"] = correct / total_feedback
            
            # Trigger retraining if needed
            if await self._should_retrain():
                await self._retrain_with_feedback()
                
        except Exception as e:
            self.logger.error(f"Error in online learning update: {e}")

    def explain_detection(self, anomaly: AnomalyResult) -> AnomalyExplanation:
        """
        Provide explanation for anomaly detection
        
        Args:
            anomaly: Anomaly result to explain
            
        Returns:
            Detailed explanation of the detection
        """
        try:
            features = anomaly.feature_contributions
            thresholds = self.adaptive_thresholds
            
            explanation = AnomalyExplanation(features, thresholds)
            return explanation
            
        except Exception as e:
            self.logger.error(f"Error generating explanation: {e}")
            return AnomalyExplanation({}, {})

    async def _isolation_forest_detection(self, feature_array: np.ndarray) -> float:
        """Detect anomalies using Isolation Forest"""
        if not SKLEARN_AVAILABLE or self.isolation_forest is None:
            return 0.0
        
        try:
            # Scale features
            if self.scaler:
                feature_scaled = self.scaler.transform([feature_array])
            else:
                feature_scaled = [feature_array]
            
            # Get anomaly score
            anomaly_score = self.isolation_forest.decision_function(feature_scaled)[0]
            
            # Convert to 0-1 range (negative scores indicate anomalies)
            normalized_score = max(0.0, -anomaly_score)
            return min(normalized_score, 1.0)
            
        except Exception as e:
            self.logger.error(f"Error in isolation forest detection: {e}")
            return 0.0

    async def _statistical_detection(self, features: Dict[str, float]) -> float:
        """Statistical anomaly detection using feature statistics"""
        try:
            if not self.feature_stats:
                return 0.0
            
            anomaly_scores = []
            
            for feature_name, value in features.items():
                stats = self.feature_stats.get(feature_name, {})
                mean = stats.get('mean', 0.0)
                std = stats.get('std', 1.0)
                
                # Calculate z-score
                if std > 0:
                    z_score = abs(value - mean) / std
                    # Convert to anomaly score (higher z-score = more anomalous)
                    anomaly_score = min(z_score / 3.0, 1.0)  # 3-sigma rule
                    anomaly_scores.append(anomaly_score)
            
            if anomaly_scores:
                return np.mean(anomaly_scores)
            return 0.0
            
        except Exception as e:
            self.logger.error(f"Error in statistical detection: {e}")
            return 0.0

    async def _pattern_detection(self, features: Dict[str, float]) -> float:
        """Pattern-based anomaly detection"""
        try:
            # Simple pattern-based rules
            anomaly_score = 0.0
            
            # Check for suspicious patterns
            
            # Very fast response times (potential bot)
            response_time = features.get('response_time', 0)
            if response_time < 50:  # Less than 50ms
                anomaly_score += 0.3
            
            # High request frequency
            frequency = features.get('request_frequency', 0)
            if frequency > 10:  # More than 10 requests per window
                anomaly_score += 0.4
            
            # High error rate
            error_rate = features.get('error_rate', 0)
            if error_rate > 0.2:  # More than 20% errors
                anomaly_score += 0.5
            
            # Very regular intervals (bot-like)
            interval_std = features.get('interval_stddev', 1.0)
            if interval_std < 0.1:  # Very regular timing
                anomaly_score += 0.3
            
            return min(anomaly_score, 1.0)
            
        except Exception as e:
            self.logger.error(f"Error in pattern detection: {e}")
            return 0.0

    def _vectorize_features(self, features: Dict[str, float]) -> np.ndarray:
        """Convert feature dictionary to numpy array"""
        # Define expected features in consistent order
        expected_features = [
            'response_time', 'payload_size', 'avg_interval', 'interval_stddev',
            'request_frequency', 'error_rate', 'endpoint_diversity'
        ]
        
        vector = []
        for feature in expected_features:
            value = features.get(feature, 0.0)
            # Handle potential None values
            if value is None:
                value = 0.0
            vector.append(float(value))
        
        return np.array(vector)

    def _generate_explanation(self, features: Dict[str, float], anomaly_score: float) -> AnomalyExplanation:
        """Generate human-readable explanation for anomaly"""
        explanation = AnomalyExplanation(features, self.adaptive_thresholds)
        
        if anomaly_score > 0.7:
            explanation.explanation_text = "High anomaly score indicates significant deviation from normal behavior"
        elif anomaly_score > 0.5:
            explanation.explanation_text = "Moderate anomaly detected with some unusual patterns"
        elif anomaly_score > 0.3:
            explanation.explanation_text = "Slight behavioral anomaly detected"
        else:
            explanation.explanation_text = "Behavior appears normal"
        
        return explanation

    def _calculate_feature_contributions(self, features: Dict[str, float]) -> Dict[str, float]:
        """Calculate how much each feature contributes to anomaly detection"""
        contributions = {}
        
        for feature_name, value in features.items():
            stats = self.feature_stats.get(feature_name, {})
            mean = stats.get('mean', 0.0)
            std = stats.get('std', 1.0)
            
            if std > 0:
                # Normalized deviation from mean
                contribution = abs(value - mean) / std
                contributions[feature_name] = min(contribution / 3.0, 1.0)
            else:
                contributions[feature_name] = 0.0
        
        return contributions

    def _get_recommended_actions(self, anomaly_score: float, anomaly_types: List[AnomalyType]) -> List[str]:
        """Get recommended actions based on anomaly detection"""
        actions = []
        
        if anomaly_score > 0.8:
            actions.append("Increase monitoring intensity")
            actions.append("Apply stricter rate limits")
            actions.append("Require additional authentication")
        elif anomaly_score > 0.6:
            actions.append("Enhanced logging and monitoring")
            actions.append("Moderate rate limit reduction")
        elif anomaly_score > 0.4:
            actions.append("Monitor for continued anomalous behavior")
        
        # Type-specific recommendations
        if AnomalyType.FREQUENCY_ANOMALY in anomaly_types:
            actions.append("Implement request frequency limits")
        
        if AnomalyType.PATTERN_ANOMALY in anomaly_types:
            actions.append("Analyze request patterns for automation")
        
        return actions

    async def _add_training_sample(self, feature_array: np.ndarray, anomaly_score: float):
        """Add sample to training buffer for online learning"""
        sample = {
            'features': feature_array,
            'anomaly_score': anomaly_score,
            'timestamp': datetime.now(timezone.utc)
        }
        self.training_buffer.append(sample)

    async def _should_retrain(self) -> bool:
        """Determine if models should be retrained"""
        # Retrain if enough time has passed
        if self.last_training and datetime.now(timezone.utc) - self.last_training < self.retrain_interval:
            return False
        
        # Retrain if accuracy has dropped significantly
        if self.model_metrics["model_accuracy"] < 0.7:
            return True
        
        # Retrain if we have enough new training data
        if len(self.training_buffer) >= self.min_training_samples:
            return True
        
        return False

    async def _retrain_with_feedback(self):
        """Retrain models incorporating feedback"""
        try:
            if len(self.training_buffer) < self.min_training_samples:
                return
            
            self.logger.info("Retraining models with feedback")
            
            # Prepare training data from buffer
            historical_data = []
            for sample in self.training_buffer:
                features_dict = {}
                feature_array = sample['features']
                expected_features = [
                    'response_time', 'payload_size', 'avg_interval', 'interval_stddev',
                    'request_frequency', 'error_rate', 'endpoint_diversity'
                ]
                
                for i, feature_name in enumerate(expected_features):
                    if i < len(feature_array):
                        features_dict[feature_name] = feature_array[i]
                
                historical_data.append({'features': features_dict})
            
            # Retrain
            await self.train_models(historical_data)
            
        except Exception as e:
            self.logger.error(f"Error in retraining: {e}")

    def _initialize_ml_models(self):
        """Initialize ML models with default parameters"""
        if not SKLEARN_AVAILABLE:
            return
        
        self.isolation_forest = IsolationForest(
            contamination=0.1,
            random_state=42,
            n_estimators=100
        )
        self.scaler = StandardScaler()

    def _update_feature_stats(self, historical_data: List[Dict[str, Any]]):
        """Update feature statistics for normalization"""
        feature_values = {}
        
        for record in historical_data:
            features = record.get('features', {})
            for feature_name, value in features.items():
                if feature_name not in feature_values:
                    feature_values[feature_name] = []
                feature_values[feature_name].append(value)
        
        # Calculate statistics
        for feature_name, values in feature_values.items():
            if values:
                self.feature_stats[feature_name] = {
                    'mean': np.mean(values),
                    'std': np.std(values),
                    'min': np.min(values),
                    'max': np.max(values)
                }

    def _update_adaptive_thresholds(self, X_scaled: np.ndarray):
        """Update adaptive thresholds based on training data"""
        if X_scaled.size == 0:
            return
        
        # Calculate thresholds as 95th percentile of each feature
        for i, feature_name in enumerate(['response_time', 'payload_size', 'avg_interval', 
                                        'interval_stddev', 'request_frequency', 'error_rate', 
                                        'endpoint_diversity']):
            if i < X_scaled.shape[1]:
                feature_values = X_scaled[:, i]
                threshold = np.percentile(feature_values, 95)
                self.adaptive_thresholds[feature_name] = threshold

    async def _save_models(self):
        """Save trained models to disk"""
        if not SKLEARN_AVAILABLE:
            return
        
        try:
            models = {
                'isolation_forest': self.isolation_forest,
                'scaler': self.scaler,
                'clustering_model': self.clustering_model,
                'pca_model': self.pca_model,
                'feature_stats': self.feature_stats,
                'adaptive_thresholds': self.adaptive_thresholds,
                'model_metrics': self.model_metrics,
                'last_training': self.last_training
            }
            
            model_file = os.path.join(self.model_path, 'anomaly_models.pkl')
            with open(model_file, 'wb') as f:
                pickle.dump(models, f)
            
            self.logger.info(f"Models saved to {model_file}")
            
        except Exception as e:
            self.logger.error(f"Error saving models: {e}")

    def _load_models(self):
        """Load trained models from disk"""
        if not SKLEARN_AVAILABLE:
            return
        
        try:
            model_file = os.path.join(self.model_path, 'anomaly_models.pkl')
            if os.path.exists(model_file):
                with open(model_file, 'rb') as f:
                    models = pickle.load(f)
                
                self.isolation_forest = models.get('isolation_forest')
                self.scaler = models.get('scaler')
                self.clustering_model = models.get('clustering_model')
                self.pca_model = models.get('pca_model')
                self.feature_stats = models.get('feature_stats', {})
                self.adaptive_thresholds = models.get('adaptive_thresholds', {})
                self.model_metrics = models.get('model_metrics', self.model_metrics)
                self.last_training = models.get('last_training')
                
                self.logger.info(f"Models loaded from {model_file}")
            else:
                self.logger.info("No existing models found, will train on first use")
                
        except Exception as e:
            self.logger.error(f"Error loading models: {e}")

    async def get_model_status(self) -> Dict[str, Any]:
        """Get current model status and metrics"""
        return {
            "ml_available": SKLEARN_AVAILABLE,
            "models_trained": self.isolation_forest is not None,
            "last_training": self.last_training.isoformat() if self.last_training else None,
            "training_samples": len(self.training_buffer),
            "feedback_samples": len(self.feedback_buffer),
            "metrics": self.model_metrics.copy(),
            "feature_stats_available": bool(self.feature_stats),
            "adaptive_thresholds_count": len(self.adaptive_thresholds)
        }
"""Global Confidence Service - Phase A Implementation

This module implements the IGlobalConfidenceService interface from the microservice
architecture blueprint, providing calibrated confidence scoring with feature vector
processing, hysteresis, and confidence band classification.

Key Features:
- Calibrated confidence scoring using logistic regression
- Platt and Isotonic scaling for calibration
- Feature vector processing with validation
- Confidence bands (low, gray, high, apply, resolved)
- Hysteresis and dwell time for stability
- Model versioning and hot-swapping
- Performance metrics and calibration reporting

Implementation Notes:
- Uses scikit-learn for machine learning operations
- Supports both Platt and Isotonic calibration methods
- Thread-safe model updates and predictions
- Comprehensive error handling and validation
- SLO compliance (p95 < 50ms, 99.95% availability)
"""

import asyncio
import logging
import json
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple
from threading import RLock
import pickle
import hashlib
import numpy as np

# Machine learning imports with fallbacks
try:
    from sklearn.linear_model import LogisticRegression
    from sklearn.calibration import CalibratedClassifierCV
    from sklearn.metrics import brier_score_loss, log_loss
    from sklearn.model_selection import cross_val_score
    ML_AVAILABLE = True
except ImportError:
    ML_AVAILABLE = False
    LogisticRegression = None
    CalibratedClassifierCV = None

from faultmaven.services.microservice_interfaces.core_services import IGlobalConfidenceService
from faultmaven.models.microservice_contracts.core_contracts import (
    ConfidenceRequest, ConfidenceResponse, ConfidenceBand
)
from faultmaven.infrastructure.observability.tracing import trace
from faultmaven.exceptions import ValidationException, ServiceException


class GlobalConfidenceService(IGlobalConfidenceService):
    """
    Implementation of IGlobalConfidenceService interface
    
    Provides calibrated confidence scoring using machine learning with:
    - Feature vector processing and validation
    - Logistic regression with Platt/Isotonic calibration
    - Confidence band classification with hysteresis
    - Model versioning and performance monitoring
    - Thread-safe model updates
    """

    # Feature definitions and expected ranges
    FEATURE_DEFINITIONS = {
        'retrieval_score': (0.0, 1.0, 'Quality of knowledge base matches'),
        'provider_confidence': (0.0, 1.0, 'LLM provider confidence score'),
        'hypothesis_score': (0.0, 1.0, 'Strength of diagnostic hypothesis'),
        'validation_result': (0.0, 1.0, 'Validation agent assessment'),
        'pattern_boost': (0.0, 0.2, 'Pattern matching confidence bonus'),
        'history_slope': (-1.0, 1.0, 'Confidence trend over last 3 turns')
    }

    # Confidence band thresholds
    BAND_THRESHOLDS = {
        ConfidenceBand.LOW: (0.0, 0.5),      # < 0.5: Request clarification
        ConfidenceBand.GRAY: (0.5, 0.8),     # 0.5-0.8: Propose with caveats
        ConfidenceBand.HIGH: (0.8, 0.9),     # ≥ 0.8: Propose confidently
        ConfidenceBand.APPLY: (0.9, 0.95),   # ≥ 0.9: Apply with confirmation
        ConfidenceBand.RESOLVED: (0.95, 1.0) # ≥ 0.95: Mark as resolved
    }

    # Recommended actions for each confidence band
    BAND_ACTIONS = {
        ConfidenceBand.LOW: [
            "Request clarification from user",
            "Gather additional evidence",
            "Ask targeted questions",
            "Review problem description"
        ],
        ConfidenceBand.GRAY: [
            "Propose solution with caveats",
            "Present multiple options",
            "Request user validation",
            "Explain uncertainty factors"
        ],
        ConfidenceBand.HIGH: [
            "Propose solution confidently",
            "Provide implementation steps",
            "Include monitoring guidance",
            "Offer troubleshooting tips"
        ],
        ConfidenceBand.APPLY: [
            "Apply solution with user confirmation",
            "Execute recommended actions",
            "Monitor results closely",
            "Prepare rollback plan"
        ],
        ConfidenceBand.RESOLVED: [
            "Mark issue as resolved",
            "Document solution",
            "Archive case data",
            "Collect user feedback"
        ]
    }

    def __init__(
        self,
        calibration_method: str = "platt",
        model_version: str = "conf-v1",
        hysteresis_up_turns: int = 1,
        hysteresis_down_turns: int = 2,
        auto_recalibration_enabled: bool = True
    ):
        """
        Initialize the Global Confidence Service
        
        Args:
            calibration_method: Calibration method ('platt' or 'isotonic')
            model_version: Current model version identifier
            hysteresis_up_turns: Turns required to move up a confidence band
            hysteresis_down_turns: Turns required to move down a confidence band
            auto_recalibration_enabled: Whether to enable automatic recalibration
        """
        self._logger = logging.getLogger(self.__class__.__name__)
        self._calibration_method = calibration_method.lower()
        self._model_version = model_version
        self._hysteresis_up = hysteresis_up_turns
        self._hysteresis_down = hysteresis_down_turns
        self._auto_recalibration = auto_recalibration_enabled
        
        # Thread safety for model operations
        self._model_lock = RLock()
        
        # Model and calibration state
        self._raw_model = None
        self._calibrated_model = None
        self._model_metadata = {}
        self._calibration_metrics = {}
        self._last_calibration = None
        
        # Hysteresis state tracking (session_id -> state)
        self._hysteresis_state = {}
        
        # Performance metrics
        self._metrics = {
            'predictions_made': 0,
            'calibration_error': 0.0,
            'avg_prediction_time_ms': 0.0,
            'model_updates': 0,
            'hysteresis_adjustments': 0
        }
        
        # Initialize default model if ML is available
        if ML_AVAILABLE:
            self._initialize_default_model()
        else:
            self._logger.warning("ML libraries not available - using fallback confidence scoring")

    def _initialize_default_model(self):
        """Initialize a basic default model for immediate use"""
        try:
            with self._model_lock:
                # Create a simple logistic regression model
                self._raw_model = LogisticRegression(
                    random_state=42,
                    max_iter=1000,
                    class_weight='balanced'
                )
                
                # Generate synthetic training data for initialization
                np.random.seed(42)
                n_samples = 1000
                n_features = len(self.FEATURE_DEFINITIONS)
                
                # Create realistic synthetic feature vectors
                X = np.random.random((n_samples, n_features))
                
                # Adjust features to realistic ranges
                for i, (feature, (min_val, max_val, _)) in enumerate(self.FEATURE_DEFINITIONS.items()):
                    X[:, i] = X[:, i] * (max_val - min_val) + min_val
                
                # Generate synthetic labels based on feature combinations
                # Higher scores for better features
                feature_weights = [0.3, 0.25, 0.2, 0.15, 0.05, 0.05]  # Weights for each feature
                y_prob = np.dot(X, feature_weights) / sum(feature_weights)
                y = (y_prob > 0.5).astype(int)
                
                # Train the model
                self._raw_model.fit(X, y)
                
                # Create calibrated version
                if self._calibration_method == "isotonic":
                    self._calibrated_model = CalibratedClassifierCV(
                        self._raw_model, method="isotonic", cv=3
                    )
                else:
                    self._calibrated_model = CalibratedClassifierCV(
                        self._raw_model, method="sigmoid", cv=3
                    )
                
                self._calibrated_model.fit(X, y)
                
                # Calculate initial metrics
                y_pred_proba = self._calibrated_model.predict_proba(X)[:, 1]
                self._calibration_metrics = {
                    'ece_score': self._calculate_ece(y, y_pred_proba),
                    'brier_score': brier_score_loss(y, y_pred_proba),
                    'log_loss': log_loss(y, y_pred_proba),
                    'training_samples': n_samples,
                    'training_date': datetime.utcnow().isoformat()
                }
                
                self._model_metadata = {
                    'version': self._model_version,
                    'calibration_method': self._calibration_method,
                    'feature_count': n_features,
                    'training_date': datetime.utcnow().isoformat(),
                    'model_type': 'logistic_regression',
                    'synthetic_training': True
                }
                
                self._last_calibration = datetime.utcnow()
                self._logger.info(f"✅ Initialized default confidence model v{self._model_version}")
                
        except Exception as e:
            self._logger.error(f"Failed to initialize default model: {e}")
            self._raw_model = None
            self._calibrated_model = None

    @trace("confidence_service_score")
    async def score_confidence(self, request: ConfidenceRequest) -> ConfidenceResponse:
        """
        Compute calibrated confidence score from feature vector
        
        Args:
            request: ConfidenceRequest with feature vector and context
            
        Returns:
            ConfidenceResponse with calibrated score, confidence band,
            recommended actions, and model metadata
        """
        start_time = datetime.utcnow()
        
        try:
            # Validate request
            self._validate_confidence_request(request)
            
            # Extract and validate features
            features = self._extract_features(request.features)
            
            # Get raw confidence score
            if ML_AVAILABLE and self._calibrated_model:
                raw_score, calibrated_score = await self._predict_with_model(features)
            else:
                raw_score, calibrated_score = self._fallback_confidence_score(features)
            
            # Apply hysteresis if session context available
            session_id = request.context.get('session_id')
            if session_id:
                calibrated_score = self._apply_hysteresis(session_id, calibrated_score)
            
            # Determine confidence band
            confidence_band = self._classify_confidence_band(calibrated_score)
            
            # Get recommended actions
            recommended_actions = self.BAND_ACTIONS.get(confidence_band, [])
            
            # Calculate feature contributions if model available
            feature_contributions = {}
            if ML_AVAILABLE and self._calibrated_model:
                feature_contributions = self._calculate_feature_importance(features)
            
            # Update metrics
            processing_time = (datetime.utcnow() - start_time).total_seconds() * 1000
            self._update_prediction_metrics(processing_time)
            
            # Build response
            response = ConfidenceResponse(
                raw_score=float(raw_score),
                calibrated_score=float(calibrated_score),
                confidence_band=confidence_band,
                model_version=self._model_version,
                calibration_method=self._calibration_method,
                recommended_actions=recommended_actions,
                feature_contributions=feature_contributions,
                calibration_error=self._calibration_metrics.get('ece_score'),
                uncertainty=self._calculate_uncertainty(calibrated_score)
            )
            
            self._logger.debug(f"Computed confidence score: {calibrated_score:.3f} ({confidence_band.value})")
            return response
            
        except ValidationException:
            raise
        except Exception as e:
            self._logger.error(f"Confidence scoring failed: {e}")
            raise ServiceException(f"Confidence scoring failed: {str(e)}") from e

    async def _predict_with_model(self, features: np.ndarray) -> Tuple[float, float]:
        """Make prediction using trained model"""
        with self._model_lock:
            if not self._calibrated_model:
                raise ServiceException("Model not available")
            
            # Reshape features for prediction
            features_2d = features.reshape(1, -1)
            
            # Get raw prediction
            raw_proba = self._raw_model.predict_proba(features_2d)[0, 1]
            
            # Get calibrated prediction
            calibrated_proba = self._calibrated_model.predict_proba(features_2d)[0, 1]
            
            return float(raw_proba), float(calibrated_proba)

    def _fallback_confidence_score(self, features: np.ndarray) -> Tuple[float, float]:
        """Fallback confidence scoring when ML not available"""
        # Simple weighted average of features
        feature_weights = np.array([0.3, 0.25, 0.2, 0.15, 0.05, 0.05])
        
        # Ensure we have the right number of features
        if len(features) != len(feature_weights):
            # Pad or truncate weights as needed
            if len(features) < len(feature_weights):
                feature_weights = feature_weights[:len(features)]
            else:
                feature_weights = np.pad(feature_weights, (0, len(features) - len(feature_weights)), 'constant', constant_values=0.1)
        
        # Normalize weights
        feature_weights = feature_weights / feature_weights.sum()
        
        # Calculate weighted score
        raw_score = np.dot(features, feature_weights)
        
        # Apply simple sigmoid for calibration
        calibrated_score = 1 / (1 + np.exp(-5 * (raw_score - 0.5)))
        
        return float(raw_score), float(calibrated_score)

    def _validate_confidence_request(self, request: ConfidenceRequest):
        """Validate confidence request"""
        if not request.features:
            raise ValidationException("Features cannot be empty")
        
        # Check for required features
        required_features = set(self.FEATURE_DEFINITIONS.keys())
        provided_features = set(request.features.keys())
        
        missing_features = required_features - provided_features
        if missing_features:
            self._logger.warning(f"Missing features: {missing_features}")
            # Don't fail - we can work with partial features
        
        # Validate feature ranges
        for feature, value in request.features.items():
            if feature in self.FEATURE_DEFINITIONS:
                min_val, max_val, _ = self.FEATURE_DEFINITIONS[feature]
                if not (min_val <= value <= max_val):
                    raise ValidationException(
                        f"Feature '{feature}' value {value} not in range [{min_val}, {max_val}]"
                    )

    def _extract_features(self, feature_dict: Dict[str, float]) -> np.ndarray:
        """Extract and order features into numpy array"""
        features = []
        
        for feature_name in self.FEATURE_DEFINITIONS.keys():
            value = feature_dict.get(feature_name, 0.0)  # Default to 0.0 if missing
            features.append(value)
        
        return np.array(features, dtype=np.float32)

    def _apply_hysteresis(self, session_id: str, current_score: float) -> float:
        """Apply hysteresis to prevent rapid confidence band changes"""
        if session_id not in self._hysteresis_state:
            self._hysteresis_state[session_id] = {
                'previous_score': current_score,
                'previous_band': self._classify_confidence_band(current_score),
                'band_consistency_count': 1,
                'last_update': datetime.utcnow()
            }
            return current_score
        
        state = self._hysteresis_state[session_id]
        previous_score = state['previous_score']
        previous_band = state['previous_band']
        current_band = self._classify_confidence_band(current_score)
        
        # Check if band would change
        if current_band != previous_band:
            # Determine required consistency count
            if current_band.value > previous_band.value:  # Moving up
                required_consistency = self._hysteresis_up
            else:  # Moving down
                required_consistency = self._hysteresis_down
            
            # Check consistency count
            if state['band_consistency_count'] < required_consistency:
                # Not enough consistency - keep previous score
                state['band_consistency_count'] += 1
                adjusted_score = previous_score
                self._logger.debug(f"Hysteresis: keeping band {previous_band.value} (consistency: {state['band_consistency_count']}/{required_consistency})")
            else:
                # Enough consistency - allow change
                state['previous_score'] = current_score
                state['previous_band'] = current_band
                state['band_consistency_count'] = 1
                adjusted_score = current_score
                self._metrics['hysteresis_adjustments'] += 1
                self._logger.debug(f"Hysteresis: band change {previous_band.value} -> {current_band.value}")
        else:
            # Same band - reset consistency counter
            state['band_consistency_count'] = 1
            state['previous_score'] = current_score
            adjusted_score = current_score
        
        state['last_update'] = datetime.utcnow()
        return adjusted_score

    def _classify_confidence_band(self, score: float) -> ConfidenceBand:
        """Classify confidence score into bands"""
        for band, (min_val, max_val) in self.BAND_THRESHOLDS.items():
            if min_val <= score < max_val or (band == ConfidenceBand.RESOLVED and score >= min_val):
                return band
        
        # Fallback to LOW if no match
        return ConfidenceBand.LOW

    def _calculate_feature_importance(self, features: np.ndarray) -> Dict[str, float]:
        """Calculate feature contributions to the prediction"""
        if not ML_AVAILABLE or not self._raw_model:
            return {}
        
        try:
            with self._model_lock:
                # Get model coefficients
                coefficients = self._raw_model.coef_[0]
                
                # Calculate contributions (feature * coefficient)
                contributions = features * coefficients
                
                # Normalize to sum to 1
                total_contribution = np.abs(contributions).sum()
                if total_contribution > 0:
                    normalized_contributions = contributions / total_contribution
                else:
                    normalized_contributions = np.zeros_like(contributions)
                
                # Map back to feature names
                feature_importance = {}
                for i, feature_name in enumerate(self.FEATURE_DEFINITIONS.keys()):
                    if i < len(normalized_contributions):
                        feature_importance[feature_name] = float(normalized_contributions[i])
                
                return feature_importance
                
        except Exception as e:
            self._logger.warning(f"Failed to calculate feature importance: {e}")
            return {}

    def _calculate_uncertainty(self, score: float) -> float:
        """Calculate prediction uncertainty"""
        # Uncertainty is highest at 0.5 and lowest at extremes
        uncertainty = 4 * score * (1 - score)
        return float(uncertainty)

    def _calculate_ece(self, y_true: np.ndarray, y_prob: np.ndarray, n_bins: int = 10) -> float:
        """Calculate Expected Calibration Error"""
        try:
            bin_boundaries = np.linspace(0, 1, n_bins + 1)
            bin_lowers = bin_boundaries[:-1]
            bin_uppers = bin_boundaries[1:]
            
            ece = 0
            for bin_lower, bin_upper in zip(bin_lowers, bin_uppers):
                in_bin = (y_prob > bin_lower) & (y_prob <= bin_upper)
                prop_in_bin = in_bin.mean()
                
                if prop_in_bin > 0:
                    accuracy_in_bin = y_true[in_bin].mean()
                    avg_confidence_in_bin = y_prob[in_bin].mean()
                    ece += np.abs(avg_confidence_in_bin - accuracy_in_bin) * prop_in_bin
            
            return float(ece)
            
        except Exception:
            return 0.0

    def _update_prediction_metrics(self, processing_time_ms: float):
        """Update performance metrics"""
        self._metrics['predictions_made'] += 1
        
        # Update average processing time
        count = self._metrics['predictions_made']
        current_avg = self._metrics['avg_prediction_time_ms']
        self._metrics['avg_prediction_time_ms'] = (current_avg * (count - 1) + processing_time_ms) / count

    @trace("confidence_service_get_model_info")
    async def get_model_info(self) -> Dict[str, Any]:
        """Get confidence model information and calibration metrics"""
        try:
            with self._model_lock:
                model_info = {
                    "model_version": self._model_version,
                    "calibration_method": self._calibration_method,
                    "last_calibration": self._last_calibration.isoformat() if self._last_calibration else None,
                    "model_available": self._calibrated_model is not None,
                    "ml_backend_available": ML_AVAILABLE,
                    "feature_definitions": {
                        name: {"range": [min_val, max_val], "description": desc}
                        for name, (min_val, max_val, desc) in self.FEATURE_DEFINITIONS.items()
                    },
                    "confidence_bands": {
                        band.value: {"range": [min_val, max_val], "actions": self.BAND_ACTIONS[band]}
                        for band, (min_val, max_val) in self.BAND_THRESHOLDS.items()
                    },
                    "hysteresis_config": {
                        "up_turns": self._hysteresis_up,
                        "down_turns": self._hysteresis_down
                    },
                    "calibration_metrics": self._calibration_metrics.copy(),
                    "performance_metrics": self._metrics.copy(),
                    "model_metadata": self._model_metadata.copy()
                }
                
                return model_info
                
        except Exception as e:
            self._logger.error(f"Failed to get model info: {e}")
            raise ServiceException(f"Model info retrieval failed: {str(e)}") from e

    @trace("confidence_service_update_model")
    async def update_model(self, model_data: bytes, version: str) -> bool:
        """Update confidence model with new calibration"""
        try:
            if not ML_AVAILABLE:
                self._logger.warning("ML not available - cannot update model")
                return False
            
            with self._model_lock:
                # Deserialize model data
                model_dict = pickle.loads(model_data)
                
                # Validate model structure
                required_keys = ['raw_model', 'calibrated_model', 'metadata', 'metrics']
                if not all(key in model_dict for key in required_keys):
                    raise ValidationException(f"Invalid model data structure. Required: {required_keys}")
                
                # Backup current model
                backup = {
                    'raw_model': self._raw_model,
                    'calibrated_model': self._calibrated_model,
                    'metadata': self._model_metadata.copy(),
                    'metrics': self._calibration_metrics.copy(),
                    'version': self._model_version
                }
                
                try:
                    # Update models
                    self._raw_model = model_dict['raw_model']
                    self._calibrated_model = model_dict['calibrated_model']
                    self._model_metadata = model_dict['metadata']
                    self._calibration_metrics = model_dict['metrics']
                    self._model_version = version
                    self._last_calibration = datetime.utcnow()
                    
                    # Update metrics
                    self._metrics['model_updates'] += 1
                    
                    self._logger.info(f"✅ Updated confidence model to version {version}")
                    return True
                    
                except Exception as e:
                    # Restore backup on failure
                    self._raw_model = backup['raw_model']
                    self._calibrated_model = backup['calibrated_model']
                    self._model_metadata = backup['metadata']
                    self._calibration_metrics = backup['metrics']
                    self._model_version = backup['version']
                    
                    self._logger.error(f"Model update failed, restored backup: {e}")
                    return False
                
        except Exception as e:
            self._logger.error(f"Model update failed: {e}")
            return False

    async def health_check(self) -> Dict[str, Any]:
        """Get service health status"""
        try:
            status = "healthy"
            errors = []
            
            # Check model availability
            if not self._calibrated_model:
                status = "degraded"
                errors.append("No calibrated model available")
            
            # Check ML backend
            if not ML_AVAILABLE:
                status = "degraded"
                errors.append("ML libraries not available")
            
            # Check calibration age
            if self._last_calibration:
                age = datetime.utcnow() - self._last_calibration
                if age > timedelta(days=30):  # Recalibration recommended monthly
                    status = "degraded"
                    errors.append(f"Model calibration age: {age.days} days")
            
            # Check performance metrics
            avg_latency = self._metrics.get('avg_prediction_time_ms', 0)
            if avg_latency > 50:  # SLO: p95 < 50ms
                status = "degraded"
                errors.append(f"Average latency: {avg_latency:.2f}ms")
            
            health_info = {
                "service": "confidence_service",
                "status": status,
                "timestamp": datetime.utcnow().isoformat(),
                "version": "1.0.0",
                "model_version": self._model_version,
                "ml_available": ML_AVAILABLE,
                "model_available": self._calibrated_model is not None,
                "predictions_made": self._metrics['predictions_made'],
                "avg_latency_ms": avg_latency,
                "calibration_method": self._calibration_method,
                "last_calibration": self._last_calibration.isoformat() if self._last_calibration else None,
                "errors": errors,
                "metrics": self._metrics.copy()
            }
            
            return health_info
            
        except Exception as e:
            return {
                "service": "confidence_service",
                "status": "unhealthy",
                "timestamp": datetime.utcnow().isoformat(),
                "error": str(e)
            }

    async def ready_check(self) -> bool:
        """Check if service is ready to handle requests"""
        try:
            # Service is ready if we have some form of confidence scoring available
            # Either ML model or fallback method
            return True  # We always have fallback method
        except Exception:
            return False

    # Utility methods for monitoring and maintenance

    async def get_calibration_metrics(self) -> Dict[str, Any]:
        """Get detailed calibration metrics"""
        return {
            "timestamp": datetime.utcnow().isoformat(),
            "metrics": self._calibration_metrics.copy(),
            "performance": self._metrics.copy(),
            "hysteresis_sessions": len(self._hysteresis_state),
            "model_metadata": self._model_metadata.copy()
        }

    async def reset_hysteresis_state(self, session_id: Optional[str] = None):
        """Reset hysteresis state for specific session or all sessions"""
        if session_id:
            self._hysteresis_state.pop(session_id, None)
            self._logger.debug(f"Reset hysteresis state for session {session_id}")
        else:
            self._hysteresis_state.clear()
            self._logger.info("Reset all hysteresis state")

    async def get_prediction_statistics(self) -> Dict[str, Any]:
        """Get prediction statistics for monitoring"""
        return {
            "timestamp": datetime.utcnow().isoformat(),
            "total_predictions": self._metrics['predictions_made'],
            "avg_processing_time_ms": self._metrics['avg_prediction_time_ms'],
            "hysteresis_adjustments": self._metrics['hysteresis_adjustments'],
            "model_updates": self._metrics['model_updates'],
            "active_sessions": len(self._hysteresis_state),
            "calibration_error": self._calibration_metrics.get('ece_score', 0.0),
            "model_version": self._model_version,
            "uptime": datetime.utcnow().isoformat()
        }
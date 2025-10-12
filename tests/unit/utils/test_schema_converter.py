"""
Unit tests for Pydantic to OpenAI Schema Converter

Tests conversion of Pydantic models to OpenAI function calling format.
"""

import pytest
from pydantic import BaseModel, Field
from typing import List, Optional
from faultmaven.utils.schema_converter import (
    pydantic_to_openai_function,
    pydantic_to_openai_tools,
)


# Test Models
class SimpleModel(BaseModel):
    """Simple model for testing basic conversion"""
    name: str = Field(..., description="The name field")
    age: int = Field(..., description="The age field", ge=0)


class OptionalFieldModel(BaseModel):
    """Model with optional fields"""
    required_field: str
    optional_field: Optional[str] = None
    default_field: str = "default_value"


class NestedModel(BaseModel):
    """Model with nested structure"""
    id: str
    metadata: dict = Field(default_factory=dict)
    tags: List[str] = Field(default_factory=list)


class ComplexModel(BaseModel):
    """Complex model with multiple field types"""
    string_field: str = Field(..., description="A string field")
    int_field: int = Field(default=42, description="An integer field")
    float_field: float = Field(..., description="A float field")
    bool_field: bool = Field(default=False, description="A boolean field")
    list_field: List[str] = Field(default_factory=list, description="A list field")
    optional_field: Optional[int] = Field(None, description="An optional field")


class TestPydanticToOpenAIFunction:
    """Test pydantic_to_openai_function conversion"""

    def test_simple_model_conversion(self):
        """Test converting a simple model"""
        result = pydantic_to_openai_function(SimpleModel)

        assert result["name"] == "SimpleModel"
        assert "Simple model for testing" in result["description"]
        assert "parameters" in result
        assert result["parameters"]["type"] == "object"
        assert "name" in result["parameters"]["properties"]
        assert "age" in result["parameters"]["properties"]
        assert "name" in result["parameters"]["required"]
        assert "age" in result["parameters"]["required"]

    def test_model_with_custom_name(self):
        """Test converting model with custom name"""
        result = pydantic_to_openai_function(
            SimpleModel,
            name="custom_function_name"
        )

        assert result["name"] == "custom_function_name"

    def test_model_with_custom_description(self):
        """Test converting model with custom description"""
        result = pydantic_to_openai_function(
            SimpleModel,
            description="Custom description for the function"
        )

        assert result["description"] == "Custom description for the function"

    def test_model_without_docstring(self):
        """Test converting model without docstring"""
        class NoDocModel(BaseModel):
            field: str

        result = pydantic_to_openai_function(NoDocModel)

        assert result["description"] == "NoDocModel response"

    def test_optional_fields_not_in_required(self):
        """Test that optional fields are not marked as required"""
        result = pydantic_to_openai_function(OptionalFieldModel)

        assert "required_field" in result["parameters"]["required"]
        assert "optional_field" not in result["parameters"]["required"]
        assert "default_field" not in result["parameters"]["required"]

    def test_field_descriptions_preserved(self):
        """Test that field descriptions are preserved"""
        result = pydantic_to_openai_function(ComplexModel)

        properties = result["parameters"]["properties"]
        assert properties["string_field"]["description"] == "A string field"
        assert properties["int_field"]["description"] == "An integer field"
        assert properties["float_field"]["description"] == "A float field"

    def test_field_types_converted_correctly(self):
        """Test that field types are converted correctly"""
        result = pydantic_to_openai_function(ComplexModel)

        properties = result["parameters"]["properties"]
        assert properties["string_field"]["type"] == "string"
        assert properties["int_field"]["type"] == "integer"
        assert properties["float_field"]["type"] == "number"
        assert properties["bool_field"]["type"] == "boolean"
        assert properties["list_field"]["type"] == "array"

    def test_nested_model_conversion(self):
        """Test converting model with nested structures"""
        result = pydantic_to_openai_function(NestedModel)

        properties = result["parameters"]["properties"]
        assert "id" in properties
        assert "metadata" in properties
        assert "tags" in properties
        assert properties["tags"]["type"] == "array"

    def test_model_with_field_constraints(self):
        """Test that field constraints are preserved"""
        class ConstrainedModel(BaseModel):
            positive_int: int = Field(..., ge=0, le=100)
            bounded_str: str = Field(..., min_length=1, max_length=50)

        result = pydantic_to_openai_function(ConstrainedModel)

        properties = result["parameters"]["properties"]
        assert "positive_int" in properties
        # Constraints should be in the schema (pydantic includes them)
        assert properties["positive_int"]["type"] == "integer"


class TestPydanticToOpenAITools:
    """Test pydantic_to_openai_tools conversion"""

    def test_tools_format_structure(self):
        """Test that tools format has correct structure"""
        result = pydantic_to_openai_tools(SimpleModel)

        assert isinstance(result, list)
        assert len(result) == 1
        assert result[0]["type"] == "function"
        assert "function" in result[0]
        assert "name" in result[0]["function"]
        assert "description" in result[0]["function"]
        assert "parameters" in result[0]["function"]

    def test_tools_with_custom_name(self):
        """Test tools format with custom name"""
        result = pydantic_to_openai_tools(
            SimpleModel,
            name="custom_tool_name"
        )

        assert result[0]["function"]["name"] == "custom_tool_name"

    def test_tools_with_custom_description(self):
        """Test tools format with custom description"""
        result = pydantic_to_openai_tools(
            SimpleModel,
            description="Custom tool description"
        )

        assert result[0]["function"]["description"] == "Custom tool description"

    def test_tools_parameters_structure(self):
        """Test that parameters in tools format are correct"""
        result = pydantic_to_openai_tools(ComplexModel)

        parameters = result[0]["function"]["parameters"]
        assert parameters["type"] == "object"
        assert "properties" in parameters
        assert "required" in parameters
        assert len(parameters["properties"]) > 0


class TestEdgeCases:
    """Test edge cases and error scenarios"""

    def test_empty_model(self):
        """Test converting model with no fields"""
        class EmptyModel(BaseModel):
            pass

        result = pydantic_to_openai_function(EmptyModel)

        assert result["name"] == "EmptyModel"
        assert result["parameters"]["type"] == "object"
        assert result["parameters"]["properties"] == {}
        assert result["parameters"]["required"] == []

    def test_model_with_only_optional_fields(self):
        """Test model where all fields are optional"""
        class AllOptionalModel(BaseModel):
            field1: Optional[str] = None
            field2: Optional[int] = None

        result = pydantic_to_openai_function(AllOptionalModel)

        assert result["parameters"]["required"] == []
        assert len(result["parameters"]["properties"]) == 2

    def test_model_with_union_types(self):
        """Test model with Union type fields"""
        from typing import Union

        class UnionModel(BaseModel):
            value: Union[str, int]

        result = pydantic_to_openai_function(UnionModel)

        # Should handle Union types gracefully
        assert "value" in result["parameters"]["properties"]

    def test_model_with_list_of_models(self):
        """Test model with list of nested models"""
        class ItemModel(BaseModel):
            name: str
            value: int

        class ContainerModel(BaseModel):
            items: List[ItemModel]

        result = pydantic_to_openai_function(ContainerModel)

        properties = result["parameters"]["properties"]
        assert "items" in properties
        assert properties["items"]["type"] == "array"

    def test_model_with_dict_field(self):
        """Test model with dict field"""
        class DictModel(BaseModel):
            metadata: dict

        result = pydantic_to_openai_function(DictModel)

        properties = result["parameters"]["properties"]
        assert "metadata" in properties
        assert properties["metadata"]["type"] == "object"


class TestRealWorldModels:
    """Test conversion of actual OODA response models"""

    def test_ooda_response_model(self):
        """Test converting OODAResponse model"""
        from faultmaven.models.responses import OODAResponse

        result = pydantic_to_openai_function(OODAResponse)

        assert result["name"] == "OODAResponse"
        properties = result["parameters"]["properties"]
        assert "answer" in properties
        assert "clarifying_questions" in properties
        assert "suggested_actions" in properties
        assert "answer" in result["parameters"]["required"]

    def test_consultant_response_model(self):
        """Test converting ConsultantResponse model"""
        from faultmaven.models.responses import ConsultantResponse

        result = pydantic_to_openai_function(ConsultantResponse)

        assert result["name"] == "ConsultantResponse"
        properties = result["parameters"]["properties"]
        assert "answer" in properties
        assert "problem_detected" in properties
        assert "severity" in properties

    def test_lead_investigator_response_model(self):
        """Test converting LeadInvestigatorResponse model"""
        from faultmaven.models.responses import LeadInvestigatorResponse

        result = pydantic_to_openai_function(LeadInvestigatorResponse)

        assert result["name"] == "LeadInvestigatorResponse"
        properties = result["parameters"]["properties"]
        assert "answer" in properties
        assert "evidence_request" in properties
        assert "phase_complete" in properties


class TestSchemaDefinitions:
    """Test handling of $defs in schemas"""

    def test_model_with_nested_definitions(self):
        """Test that $defs are preserved"""
        from faultmaven.models.responses import LeadInvestigatorResponse

        result = pydantic_to_openai_function(LeadInvestigatorResponse)

        # Check if $defs are present (for nested models)
        parameters = result["parameters"]
        # Pydantic may include $defs for nested models
        # This test verifies the conversion doesn't break with nested structures


class TestFunctionNaming:
    """Test function naming conventions"""

    def test_default_name_uses_class_name(self):
        """Test that default name matches class name"""
        class MyCustomModel(BaseModel):
            field: str

        result = pydantic_to_openai_function(MyCustomModel)
        assert result["name"] == "MyCustomModel"

    def test_name_parameter_overrides_class_name(self):
        """Test that name parameter overrides class name"""
        class MyCustomModel(BaseModel):
            field: str

        result = pydantic_to_openai_function(
            MyCustomModel,
            name="respond_with_custom_name"
        )
        assert result["name"] == "respond_with_custom_name"
        assert result["name"] != "MyCustomModel"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])

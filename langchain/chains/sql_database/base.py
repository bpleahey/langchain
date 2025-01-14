"""Chain for interacting with SQL Database."""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Extra, Field

# import other specific exceptions
from sqlalchemy.exc import InvalidRequestError, OperationalError

from langchain.chains.base import Chain
from langchain.chains.llm import LLMChain
from langchain.chains.sql_database.prompt import DECIDER_PROMPT, PROMPT
from langchain.llms.base import BaseLLM
from langchain.prompts.base import BasePromptTemplate
from langchain.sql_database import SQLDatabase


class SQLDatabaseChain(Chain, BaseModel):
    """Chain for interacting with SQL Database.

    Example:
        .. code-block:: python

            from langchain import SQLDatabaseChain, OpenAI, SQLDatabase
            db = SQLDatabase(...)
            db_chain = SQLDatabaseChain(llm=OpenAI(), database=db)
    """

    llm: BaseLLM
    """LLM wrapper to use."""
    database: SQLDatabase = Field(exclude=True)
    """SQL Database to connect to."""
    max_tries: int = Field(1, ge=1)
    """Maximum number of times to try to run a query before giving up."""
    prompt: BasePromptTemplate = PROMPT
    """Prompt to use to translate natural language to SQL."""
    top_k: int = 5
    """Number of results to return from the query"""
    input_key: str = "query"  #: :meta private:
    output_key: str = "result"  #: :meta private:
    return_intermediate_steps: bool = False
    """Whether or not to return the intermediate steps along with the final answer."""
    return_direct: bool = False
    """Whether or not to return the result of querying the SQL table directly."""

    class Config:
        """Configuration for this pydantic object."""

        extra = Extra.forbid
        arbitrary_types_allowed = True

    @property
    def input_keys(self) -> List[str]:
        """Return the singular input key.

        :meta private:
        """
        return [self.input_key]

    @property
    def output_keys(self) -> List[str]:
        """Return the singular output key.

        :meta private:
        """
        if not self.return_intermediate_steps:
            return [self.output_key]
        else:
            return [self.output_key, "intermediate_steps"]

    def _call(self, inputs: Dict[str, Any]) -> Dict[str, Any]:
        llm_chain = LLMChain(llm=self.llm, prompt=self.prompt)
        input_text = f"{inputs[self.input_key]} \nSQLQuery:"
        self.callback_manager.on_text(input_text, verbose=self.verbose)
        # If not present, then defaults to None which is all tables.
        table_names_to_use = inputs.get("table_names_to_use")
        table_info = self.database.get_table_info(table_names=table_names_to_use)
        llm_inputs = {
            "input": input_text,
            "top_k": self.top_k,
            "dialect": self.database.dialect,
            "table_info": table_info,
            "stop": ["\nSQLResult:"],
        }
        intermediate_steps = []
        sql_cmd = llm_chain.predict(**llm_inputs)
        intermediate_steps.append(sql_cmd)
        self.callback_manager.on_text(sql_cmd, color="green", verbose=self.verbose)

        try:
            result = self.database.run(sql_cmd)
        except Exception as e:
            result = self.handle_exception(llm_inputs, llm_chain, e)
        
        intermediate_steps.append(result)
        self.callback_manager.on_text("\nSQLResult: ", verbose=self.verbose)
        self.callback_manager.on_text(result, color="yellow", verbose=self.verbose)
        # If return direct, we just set the final result equal to the sql query
        if self.return_direct:
            final_result = result
        else:
            self.callback_manager.on_text("\nAnswer:", verbose=self.verbose)
            input_text += f"{sql_cmd}\nSQLResult: {result}\nAnswer:"
            llm_inputs["input"] = input_text
            final_result = llm_chain.predict(**llm_inputs)
            self.callback_manager.on_text(
                final_result, color="green", verbose=self.verbose
            )
        chain_result: Dict[str, Any] = {self.output_key: final_result}
        if self.return_intermediate_steps:
            chain_result["intermediate_steps"] = intermediate_steps
        return chain_result

    # TODO: may want to rename this to something more specific once we have
    # more than one exception handler

    def handle_exception(
        self,
        llm_inputs: Dict[str, Any],
        llm_chain: LLMChain,
        exception: Exception,
    ) -> str:
        """
        Method for handling exceptions for the SQLDatabaseChain class.

        see list of exceptions here:
        https://docs.sqlalchemy.org/en/20/core/exceptions.html

        key:
        InvalidRequestError
        """
        for i in range(self.max_tries):
            if isinstance(exception, OperationalError):
                print("OperationalError")
                raise exception

                """
                produced outputs (giving direct SQL commands):
                wrong number of arguments to function SUM() (or others like AVG)
                no such table ___
                ambiguous column name: ___

                produced outputs (giving natural language):
                no such table ___
                
                """
                try:
                    return llm_chain.predict(**llm_inputs)
                except Exception as e:
                    exception = e
                # TODO: this is the most common error, so we should handle
                # it in a more specific way
        # Use specific exception here (check langchain specific exceptions and
        #  general python exceptions)
        raise exception  # find langchain specific exception to raise here

    # TODO: other implementations of handle, including logical errors


class SQLDatabaseSequentialChain(Chain, BaseModel):
    """Chain for querying SQL database that is a sequential chain.

    The chain is as follows:
    1. Based on the query, determine which tables to use.
    2. Based on those tables, call the normal SQL database chain.

    This is useful in cases where the number of tables in the database is large.
    """

    @classmethod
    def from_llm(
        cls,
        llm: BaseLLM,
        database: SQLDatabase,
        query_prompt: BasePromptTemplate = PROMPT,
        decider_prompt: BasePromptTemplate = DECIDER_PROMPT,
        **kwargs: Any,
    ) -> SQLDatabaseSequentialChain:
        """Load the necessary chains."""
        # TODO: experiment with chainging max_tries
        sql_chain = SQLDatabaseChain(
            llm=llm, database=database, prompt=query_prompt, **kwargs
        )
        decider_chain = LLMChain(
            llm=llm, prompt=decider_prompt, output_key="table_names"
        )
        return cls(sql_chain=sql_chain, decider_chain=decider_chain, **kwargs)

    decider_chain: LLMChain
    sql_chain: SQLDatabaseChain
    input_key: str = "query"  #: :meta private:
    output_key: str = "result"  #: :meta private:

    @property
    def input_keys(self) -> List[str]:
        """Return the singular input key.

        :meta private:
        """
        return [self.input_key]

    @property
    def output_keys(self) -> List[str]:
        """Return the singular output key.

        :meta private:
        """
        return [self.output_key]

    def _call(self, inputs: Dict[str, str]) -> Dict[str, str]:
        _table_names = self.sql_chain.database.get_table_names()
        table_names = ", ".join(_table_names)
        llm_inputs = {
            "query": inputs[self.input_key],
            "table_names": table_names,
        }
        table_names_to_use = self.decider_chain.predict_and_parse(**llm_inputs)
        self.callback_manager.on_text(
            "Table names to use:", end="\n", verbose=self.verbose
        )
        self.callback_manager.on_text(
            str(table_names_to_use), color="yellow", verbose=self.verbose
        )
        new_inputs = {
            self.sql_chain.input_key: inputs[self.input_key],
            "table_names_to_use": table_names_to_use,
        }
        return self.sql_chain(new_inputs, return_only_outputs=True)

    @property
    def _chain_type(self) -> str:
        return "sql_database_sequential_chain"

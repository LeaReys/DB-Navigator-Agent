from agent.graph import run

if __name__ == "__main__":
    test_queries = [
        "Где найти информацию по статусу должника?",    # → NAVIGATION
        "Какая структура таблицы debt?",                # → SCHEMA
        "Напиши скрипт для последней даты платежа",     # → SCRIPT
        "Какой статус у должника с id 123?",            # → DATA
    ]

    for query in test_queries:
        print("\n" + "═" * 60)
        print(f"ЗАПРОС: {query}")
        print("═" * 60)

        result = run(query)

        print(f"\n= РЕЗУЛЬТАТ =")
        print(f"Шаги: {result.get('steps', [])}")

        final = result.get("final_response")
        if final:
            print(f"Тип: {final.query_type}")
            print(f"Ответ:\n{final.answer}")
            if final.sql:
                print(f"SQL:\n{final.sql}")
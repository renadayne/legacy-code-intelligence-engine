#include "model.h"

int Validate(int value) { return value > 0 ? value : 0; }
double Validate(double value) { return value > 0.0 ? value : 0.0; }

int SaveOrder(int value) { return Validate(value); }
int OrderService::Commit(int value) { return SaveOrder(value); }

#pragma once

int Validate(int value);
double Validate(double value);
int SaveOrder(int value);

struct OrderService {
    virtual int Commit(int value);
};

class COrderDlg {
public:
    void OnSave();
    void OnCancel();
    int lastResult = 0;
};
